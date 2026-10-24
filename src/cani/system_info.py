"""Host and sandbox introspection for the system summary ribbon."""

from __future__ import annotations

import os
import platform
import pwd
import socket
from collections.abc import Callable, Mapping

from pydantic import BaseModel

# Linux capability table, indexed by bit number (include/uapi/linux/capability.h).
CAPABILITY_NAMES = [
    "CHOWN", "DAC_OVERRIDE", "DAC_READ_SEARCH", "FOWNER", "FSETID", "KILL",
    "SETGID", "SETUID", "SETPCAP", "LINUX_IMMUTABLE", "NET_BIND_SERVICE",
    "NET_BROADCAST", "NET_ADMIN", "NET_RAW", "IPC_LOCK", "IPC_OWNER",
    "SYS_MODULE", "SYS_RAWIO", "SYS_CHROOT", "SYS_PTRACE", "SYS_PACCT",
    "SYS_ADMIN", "SYS_BOOT", "SYS_NICE", "SYS_RESOURCE", "SYS_TIME",
    "SYS_TTY_CONFIG", "MKNOD", "LEASE", "AUDIT_WRITE", "AUDIT_CONTROL",
    "SETFCAP", "MAC_OVERRIDE", "MAC_ADMIN", "SYSLOG", "WAKE_ALARM",
    "BLOCK_SUSPEND", "AUDIT_READ", "PERFMON", "BPF", "CHECKPOINT_RESTORE",
]

SECCOMP_MODES = {0: "disabled", 1: "strict", 2: "filter"}


class SystemInfo(BaseModel):
    hostname: str
    kernel: str
    uid: int
    user: str
    capabilities: list[str]
    container_runtime: str | None = None
    apparmor: str = "unknown"  # host LSM; OpenShell itself uses Landlock, not AppArmor
    seccomp: str = "unknown"
    landlock_abi: int | None = None  # Landlock ABI version, None if unsupported
    openshell: bool = False  # running inside an OpenShell sandbox
    sandboxed: bool = False


# landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION) returns the
# highest supported Landlock ABI version, or -1/ENOSYS where unsupported.
# Syscall number is 444 on both x86_64 and aarch64.
_LANDLOCK_SYSCALL_NR = 444
_LANDLOCK_CREATE_RULESET_VERSION = 1


def landlock_abi_version(syscall_fn=None) -> int | None:
    """Return the kernel's Landlock ABI version, or None if unsupported."""
    if syscall_fn is None:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.syscall.restype = ctypes.c_long

        def syscall_fn(nr, *args):  # noqa: ANN001
            return libc.syscall(ctypes.c_long(nr), *args)

    rc = syscall_fn(
        _LANDLOCK_SYSCALL_NR, None, 0, _LANDLOCK_CREATE_RULESET_VERSION
    )
    return rc if rc and rc > 0 else None


def detect_openshell(environ=os.environ, exists=os.path.exists) -> bool:
    """Detect execution inside an OpenShell sandbox.

    Markers observed in v0.1.5: the OPENSHELL_SANDBOX env var and the
    supervisor CA material dir. /etc/openshell kept as a fallback for
    images that bake a policy there.
    """
    if environ.get("OPENSHELL_SANDBOX"):
        return True
    return bool(exists("/run/openshell-supervisor-ca") or exists("/etc/openshell"))


def decode_capabilities(mask: int) -> list[str]:
    """Decode a CapEff-style bitmask into capability names."""
    names = []
    bit = 0
    while mask:
        if mask & 1:
            if bit < len(CAPABILITY_NAMES):
                names.append(CAPABILITY_NAMES[bit])
            else:
                names.append(f"UNKNOWN_{bit}")
        mask >>= 1
        bit += 1
    return names


def parse_seccomp_mode(mode: int) -> str:
    return SECCOMP_MODES.get(mode, "unknown")


def detect_container(
    exists: Callable[[str], bool] = os.path.exists,
    environ: Mapping[str, str] = os.environ,
) -> str | None:
    """Return the container runtime name, or None on a bare host."""
    if exists("/run/.containerenv"):
        return "podman"
    if exists("/.dockerenv"):
        return "docker"
    runtime = environ.get("container")
    return runtime or None


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return ""


def _proc_status_field(field: str) -> str:
    for line in _read("/proc/self/status").splitlines():
        if line.startswith(field + ":"):
            return line.split(":", 1)[1].strip()
    return ""


def _apparmor_profile() -> str:
    # "profile-name (enforce)" -> keep as-is; empty means LSM absent/unconfined
    current = _read("/proc/self/attr/current")
    return current or "unconfined"


def gather() -> SystemInfo:
    cap_eff_hex = _proc_status_field("CapEff")
    try:
        cap_mask = int(cap_eff_hex, 16)
    except ValueError:
        cap_mask = 0
    seccomp_raw = _proc_status_field("Seccomp")
    seccomp = parse_seccomp_mode(int(seccomp_raw)) if seccomp_raw.isdigit() else "unknown"
    runtime = detect_container()
    apparmor = _apparmor_profile()
    openshell = detect_openshell()
    sandboxed = (
        openshell
        or runtime is not None
        or seccomp == "filter"
        or (apparmor not in ("unconfined", "unknown"))
    )
    return SystemInfo(
        hostname=socket.gethostname(),
        kernel=platform.release(),
        uid=os.getuid(),
        user=pwd.getpwuid(os.getuid()).pw_name,
        capabilities=decode_capabilities(cap_mask),
        container_runtime=runtime,
        apparmor=apparmor,
        seccomp=seccomp,
        landlock_abi=landlock_abi_version(),
        openshell=openshell,
        sandboxed=sandboxed,
    )
