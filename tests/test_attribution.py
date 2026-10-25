"""Tests for the which-layer-denied-it heuristics."""
import os
import signal
import stat

import pytest

from cani.attribution import attribute, dac_denies
from cani.probes import ProbeResult, Status
from cani.system_info import SystemInfo

HOST = SystemInfo(hostname="h", kernel="6.12", uid=1000, user="u", capabilities=[])
OPENSHELL = HOST.model_copy(update={"openshell": True, "sandboxed": True, "seccomp": "filter",
                                    "landlock_abi": 6})


def res(command, category="filesystem", code=1, stderr="", detail="",
        status=Status.DENIED, name="probe"):
    return ProbeResult(id="x", name=name, category=category, command=command,
                       status=status, exit_code=code, stderr=stderr, detail=detail)


def fake_stat(mode, uid=0, gid=0):
    def _stat(path):
        return os.stat_result((mode, 0, 0, 1, uid, gid, 0, 0, 0, 0))
    return _stat


def test_allowed_and_running_have_no_blocker():
    assert attribute(res("true", code=0, status=Status.ALLOWED), HOST) is None
    assert attribute(res("true", code=None, status=Status.RUNNING), HOST) is None


def test_spawn_error():
    b = attribute(res("x", code=None, status=Status.ERROR, detail="boom"), HOST)
    assert b.layer == "spawn failed" and "boom" in b.reason


def test_missing_binary_is_baseline_not_policy():
    b = attribute(res("nmap --version", "process", 127, "sh: nmap: command not found"), OPENSHELL)
    assert (b.layer, b.kind) == ("not installed", "baseline")


@pytest.mark.parametrize("code", [-signal.SIGSYS, 128 + signal.SIGSYS])
def test_sigsys_is_seccomp(code):
    b = attribute(res("unshare -U true", "process", code), HOST)
    assert (b.layer, b.kind) == ("seccomp", "sandbox")


def test_unshare_eperm_under_seccomp_filter():
    b = attribute(res("unshare --user true", "process", 1,
                      "unshare: unshare failed: Operation not permitted"), OPENSHELL)
    assert b.layer == "seccomp"


def test_unshare_eperm_on_plain_host_is_capabilities():
    b = attribute(res("unshare --user true", "process", 1,
                      "unshare: unshare failed: Operation not permitted"), HOST)
    assert (b.layer, b.kind) == ("capabilities", "baseline")


def test_openshell_proxy_403():
    b = attribute(res("curl -fsS https://opensuse.org", "network", 56,
                      "curl: (56) CONNECT tunnel failed, response 403"), OPENSHELL)
    assert (b.layer, b.kind) == ("OpenShell egress", "sandbox")


def test_proxy_403_outside_openshell():
    b = attribute(res("curl -fsS https://x", "network", 56, "CONNECT tunnel failed, response 403"), HOST)
    assert b.layer == "proxy"


def test_dns_failure():
    b = attribute(res("curl https://nope", "network", 6, "curl: (6) Could not resolve host: nope"), HOST)
    assert (b.layer, b.kind) == ("DNS", "baseline")
    assert attribute(res("getent hosts opensuse.org", "network", 2), OPENSHELL).layer == "DNS"


def test_ping_without_net_raw():
    b = attribute(res("ping -c1 1.1.1.1", "network", 2,
                      "ping: socket: Operation not permitted"), OPENSHELL)
    assert (b.layer, b.kind) == ("CAP_NET_RAW", "sandbox")


def test_timeout_is_dropped():
    b = attribute(res("curl http://169.254.169.254/", "network", -9, detail="timed out after 3s"), OPENSHELL)
    assert (b.layer, b.kind) == ("dropped", "sandbox")


def test_connection_refused_inside_openshell():
    b = attribute(res("curl https://x", "network", 7, "curl: (7) Failed to connect"), OPENSHELL)
    assert b.layer == "OpenShell egress"
    assert attribute(res("curl https://x", "network", 7, "Failed to connect"), HOST).layer == "unreachable"


def test_sudo_variants():
    nnp = attribute(res("sudo -n true", "process", 1,
                        "sudo: The \"no new privileges\" flag is set"), OPENSHELL)
    assert nnp.layer == "no_new_privs"
    pw = attribute(res("sudo -n true", "process", 1, "sudo: a password is required"), HOST)
    assert (pw.layer, pw.kind) == ("sudoers", "baseline")


def test_read_only_fs():
    b = attribute(res("touch /etc/x", stderr="touch: cannot touch '/etc/x': Read-only file system"), OPENSHELL)
    assert b.layer == "read-only fs"


def test_shadow_denied_by_mode_bits_even_in_sandbox():
    # /etc/shadow is 0640 root:shadow — unix perms say no before Landlock is consulted
    b = attribute(res("head -c1 /etc/shadow",
                      stderr="head: cannot open '/etc/shadow' for reading: Permission denied"),
                  OPENSHELL, stat_fn=fake_stat(stat.S_IFREG | 0o640, uid=0, gid=42), gids={1000})
    assert (b.layer, b.kind) == ("file perms", "baseline")


def test_eacces_despite_open_mode_bits_is_landlock_in_openshell():
    b = attribute(res("head -c1 /data/file",
                      stderr="head: cannot open '/data/file' for reading: Permission denied"),
                  OPENSHELL, stat_fn=fake_stat(stat.S_IFREG | 0o644), gids={1000})
    assert (b.layer, b.kind) == ("Landlock", "sandbox")


def test_exec_denied_126():
    b = attribute(res("sh -c '/tmp/.can-i-x'", code=126,
                      stderr="sh: 1: /tmp/.can-i-x: Permission denied", name="Execute from /tmp"), OPENSHELL)
    assert b.layer == "Landlock"
    assert attribute(res("x", code=126, stderr="sh: /tmp/y: Permission denied"), HOST).layer == "noexec"


def test_unrecognized_failure():
    b = attribute(res("false", "process", 1), HOST)
    assert (b.layer, b.kind) == ("exit 1", "unknown")


def test_dac_denies_write_to_missing_file_checks_parent():
    calls = []

    def st(path):
        calls.append(path)
        if path == "/etc/new":
            raise FileNotFoundError(path)
        return os.stat_result((stat.S_IFDIR | 0o755, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    assert dac_denies("/etc/new", True, 1000, {1000}, st) is True
    assert calls == ["/etc/new", "/etc"]
    assert dac_denies("/etc/new", True, 0, set(), st) is False


async def test_run_attaches_blocker(tmp_path):
    from cani.state import ProbeState

    p = tmp_path / "checks.yaml"
    p.write_text("checks:\n  - id: gone\n    name: gone\n    category: process\n"
                 "    command: definitely-not-a-real-binary-xyz\n")
    state = ProbeState(p)
    await state.run_now()
    blocker = state.snapshot()["results"][0]["blocker"]
    assert blocker["layer"] == "not installed"


def test_ungranted_binary_connect_eacces_is_egress_not_landlock():
    # observed in OpenShell v0.1.5: python3 -> suse.com when only curl is granted
    b = attribute(res("python3 -c 'import urllib.request; ...'", "network", 1,
                      "urllib.error.URLError: <urlopen error [Errno 13] Permission denied>"), OPENSHELL)
    assert (b.layer, b.kind) == ("OpenShell egress", "sandbox")


def test_openshell_blocked_destination_observed_stderr():
    # observed in OpenShell v0.1.5 for a host missing from network_policies
    b = attribute(res("curl -fsS https://opensuse.org", "network", 7,
                      "curl: (7) Failed to connect to opensuse.org port 443 after 2 ms: "
                      "Could not connect to server"), OPENSHELL)
    assert b.layer == "OpenShell egress"
