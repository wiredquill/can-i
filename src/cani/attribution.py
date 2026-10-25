"""Best-guess attribution: which enforcement layer denied a probe.

A probe only tells us *that* an action failed. For a demo the interesting
question is *who* said no: the sandbox policy (Landlock, seccomp, the
OpenShell egress proxy, dropped capabilities) or the baseline environment
(unix file permissions, a binary that simply isn't installed, sudoers).

This is a heuristic over stderr, the exit code and the sandbox context —
the kernel does not report which LSM or filter produced an errno — so the
reason text says what evidence the guess rests on.
"""

from __future__ import annotations

import os
import re
import signal
import stat
from collections.abc import Callable

from .probes import Blocker, ProbeResult, Status
from .system_info import SystemInfo

SIGSYS = int(signal.SIGSYS)  # 31 on Linux


# paths quoted by coreutils/sh in their error messages: '/etc/shadow' or ‘/etc/shadow’
_QUOTED_PATH = re.compile(r"['‘`\"](/[^'’`\"]+)['’`\"]")
# sh: /tmp/.can-i-x: Permission denied
_SH_PATH = re.compile(r"(/[^\s:]+): (?:permission denied|operation not permitted)", re.I)


def _denied_path(text: str) -> str | None:
    m = _QUOTED_PATH.search(text) or _SH_PATH.search(text)
    return m.group(1) if m else None


def dac_denies(
    path: str,
    write: bool,
    uid: int,
    gids: set[int],
    stat_fn: Callable[[str], os.stat_result] = os.stat,
) -> bool | None:
    """Would plain unix mode bits deny this access? None when we can't tell.

    For a write to a path that doesn't exist yet (touch), the parent
    directory's write bit is what counts.
    """
    if uid == 0:
        return False
    try:
        st = stat_fn(path)
    except FileNotFoundError:
        if not write:
            return None
        try:
            st = stat_fn(os.path.dirname(path) or "/")
        except OSError:
            return None
    except OSError:
        return None
    if st.st_uid == uid:
        bit = stat.S_IWUSR if write else stat.S_IRUSR
    elif st.st_gid in gids:
        bit = stat.S_IWGRP if write else stat.S_IRGRP
    else:
        bit = stat.S_IWOTH if write else stat.S_IROTH
    return not (st.st_mode & bit)


def _is_write(command: str, text: str) -> bool:
    return bool(re.search(r"\b(touch|tee|mkdir|cp|mv|rm|>)\b|cannot (touch|create)", command + " " + text))


def attribute(
    result: ProbeResult,
    system: SystemInfo,
    stat_fn: Callable[[str], os.stat_result] = os.stat,
    gids: set[int] | None = None,
) -> Blocker | None:
    """Explain a denied/error result; None for allowed or still-running probes."""
    if result.status in (Status.ALLOWED, Status.RUNNING):
        return None
    if result.status == Status.ERROR:
        return Blocker(layer="spawn failed", kind="unknown",
                       reason=f"the probe could not start: {result.detail or 'unknown error'}")

    text = f"{result.stderr}\n{result.detail}".lower()
    cmd = result.command.strip()
    code = result.exit_code
    sandboxed = system.sandboxed
    policy_kind = "sandbox" if sandboxed else "baseline"

    if code == 127 or "command not found" in text or re.search(r": not found\b", text):
        return Blocker(layer="not installed", kind="baseline",
                       reason="exit 127: the binary isn't on PATH — absent from the image, not a policy decision")

    if code in (-SIGSYS, 128 + SIGSYS) or "bad system call" in text:
        return Blocker(layer="seccomp", kind="sandbox",
                       reason="killed by SIGSYS: a seccomp filter trapped a forbidden syscall")

    # ---- network ----------------------------------------------------------
    if result.category == "network":
        if "403" in text or "tunnel failed" in text or "proxy" in text:
            if system.openshell:
                return Blocker(layer="OpenShell egress", kind="sandbox",
                               reason="the sandbox's egress proxy refused this destination — it isn't in network_policies")
            return Blocker(layer="proxy", kind="baseline", reason="an HTTP proxy refused the request")
        if "could not resolve" in text or "name or service not known" in text or (
            cmd.startswith("getent") and code == 2
        ) or (cmd.startswith("curl") and code == 6):
            return Blocker(layer="DNS", kind="sandbox" if system.openshell else "baseline",
                           reason="the name did not resolve — DNS is blocked or unavailable here")
        if cmd.startswith("ping") and ("not permitted" in text or "permission denied" in text):
            return Blocker(layer="CAP_NET_RAW", kind=policy_kind,
                           reason="raw ICMP sockets need CAP_NET_RAW, which this process does not hold")
        if result.detail.startswith("timed out") or "timed out" in text or (cmd.startswith("curl") and code == 28):
            return Blocker(layer="dropped", kind="sandbox" if sandboxed else "unknown",
                           reason="no answer before the timeout — egress filtering dropped it or the host is unreachable")
        if system.openshell and ("permission denied" in text or "not permitted" in text):
            # OpenShell network policies are per-binary: an ungranted binary's connect() gets EACCES
            return Blocker(layer="OpenShell egress", kind="sandbox",
                           reason="connect() refused with EACCES — this binary isn't granted this destination")
        if "refused" in text or "failed to connect" in text or "unreachable" in text or (
            cmd.startswith("curl") and code == 7
        ):
            if system.openshell:
                return Blocker(layer="OpenShell egress", kind="sandbox",
                               reason="the connection was refused inside the sandbox — no matching network policy")
            return Blocker(layer="unreachable", kind="baseline", reason="connection refused or no route to host")

    # ---- privilege escalation -----------------------------------------------
    if "no new privileges" in text or "nosuid" in text or "effective uid is not 0" in text:
        return Blocker(layer="no_new_privs", kind=policy_kind,
                       reason="setuid binaries can't elevate here (no_new_privs or a nosuid mount)")
    if "password is required" in text or "sudoers" in text or "may not run sudo" in text:
        return Blocker(layer="sudoers", kind="baseline",
                       reason="this user has no passwordless sudo rights")

    if "read-only file system" in text:
        return Blocker(layer="read-only fs", kind=policy_kind,
                       reason="EROFS: the target is on a read-only mount")

    eperm = "operation not permitted" in text
    eacces = "permission denied" in text

    # ---- EPERM from privileged syscalls (unshare, mount, ...) ---------------
    if eperm and result.category == "process":
        if system.seccomp == "filter":
            return Blocker(layer="seccomp", kind="sandbox",
                           reason="EPERM from a privileged syscall while a seccomp filter is active")
        return Blocker(layer="capabilities", kind=policy_kind,
                       reason="EPERM: the kernel refused — a missing capability or unprivileged userns is disabled")

    # ---- EACCES / EPERM on files ---------------------------------------------
    if eacces or eperm:
        path = _denied_path(result.stderr)
        if code == 126 or (path and path.startswith("/tmp") and "exec" in result.name.lower()):
            if system.openshell:
                return Blocker(layer="Landlock", kind="sandbox",
                               reason="exec denied: the path lacks execute rights in the sandbox's filesystem policy")
            return Blocker(layer="noexec", kind=policy_kind,
                           reason="exec denied: the file isn't executable or its mount is noexec")
        if path:
            dac = dac_denies(path, _is_write(cmd, text), os.getuid(),
                             gids if gids is not None else set(os.getgroups()) | {os.getgid()}, stat_fn)
            if dac:
                return Blocker(layer="file perms", kind="baseline",
                               reason=f"unix mode bits on {path} deny this user — denied even without a sandbox")
        if system.openshell:
            return Blocker(layer="Landlock", kind="sandbox",
                           reason="EACCES although unix permissions allow it — outside the sandbox's filesystem policy")
        return Blocker(layer="file perms", kind="baseline", reason="EACCES from the kernel's permission checks")

    if "no such file or directory" in text:
        return Blocker(layer="missing", kind="baseline", reason="the target path does not exist here")

    return Blocker(layer=f"exit {code}", kind="unknown",
                   reason="the command failed without a recognizable denial signal — see stderr")
