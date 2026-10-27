"""Tests for OpenShell policy <-> checks.yaml interop."""
import re

import pytest

from cani.config import Check, load_config
from cani.policy import checks_to_policy, policy_to_checks
from cani.probes import Status, run_probe

# ---- expect field on checks and results -------------------------------------


def test_check_accepts_expect_field():
    c = Check(id="x", name="X", category="network", command="true", expect="allowed")
    assert c.expect == "allowed"


def test_check_expect_defaults_to_none():
    c = Check(id="x", name="X", category="network", command="true")
    assert c.expect is None


async def test_result_reports_expectation_met():
    c = Check(id="x", name="X", category="process", command="true", expect="allowed")
    r = await run_probe(c)
    assert r.status == Status.ALLOWED
    assert r.met is True


async def test_result_reports_expectation_unmet():
    c = Check(id="x", name="X", category="process", command="true", expect="denied")
    r = await run_probe(c)
    assert r.met is False


async def test_result_without_expectation_has_no_met():
    c = Check(id="x", name="X", category="process", command="false")
    r = await run_probe(c)
    assert r.met is None


# ---- policy -> checks ---------------------------------------------------------

POLICY = """\
version: 1
filesystem_policy:
  include_workdir: true
  read_only:
    - /etc/resolv.conf
  read_write:
    - /tmp
process:
  run_as_user: sandbox
network_policies:
  allow-suse:
    name: allow-suse
    endpoints:
      - host: suse.com
        port: 443
      - host: download.opensuse.org
        port: 443
    binaries:
      - /usr/bin/curl
"""


def test_policy_network_endpoints_become_allowed_checks():
    cfg = policy_to_checks(POLICY)
    net = [c for c in cfg.checks if c.id.startswith("net-allow-")]
    assert {c.id for c in net} == {"net-allow-suse-com", "net-allow-download-opensuse-org"}
    assert all(c.expect == "allowed" for c in net)
    assert all("curl" in c.command for c in net)


def test_policy_generates_unlisted_network_control_denied():
    cfg = policy_to_checks(POLICY)
    controls = [c for c in cfg.checks if c.id == "net-deny-unlisted-control"]
    assert len(controls) == 1
    assert controls[0].expect == "denied"


def test_policy_read_only_paths_become_read_checks():
    cfg = policy_to_checks(POLICY)
    c = next(c for c in cfg.checks if c.id == "fs-read-etc-resolv-conf")
    assert "test -r /etc/resolv.conf" in c.command
    assert c.expect == "allowed"


def test_policy_read_write_paths_become_write_checks():
    cfg = policy_to_checks(POLICY)
    c = next(c for c in cfg.checks if c.id == "fs-write-tmp")
    assert "test -w /tmp" in c.command
    assert c.expect == "allowed"


def test_policy_generates_fs_control_denied_outside_read_write():
    cfg = policy_to_checks(POLICY)
    c = next(c for c in cfg.checks if c.id == "fs-write-etc-control")
    assert c.expect == "denied"


def test_policy_run_as_user_generates_shadow_denied():
    cfg = policy_to_checks(POLICY)
    c = next(c for c in cfg.checks if c.id == "fs-read-shadow")
    assert c.expect == "denied"


def test_policy_include_workdir_generates_workdir_read_check():
    cfg = policy_to_checks(POLICY)
    assert any(c.id == "fs-read-workdir" for c in cfg.checks)


def test_generated_config_roundtrips_through_loader(tmp_path):
    cfg = policy_to_checks(POLICY)
    p = tmp_path / "checks.yaml"
    p.write_text(cfg.to_yaml())
    loaded = load_config(p)
    assert len(loaded.checks) == len(cfg.checks)


def test_empty_policy_raises():
    with pytest.raises(ValueError, match="nothing"):
        policy_to_checks("version: 1\n")


# ---- checks -> policy ---------------------------------------------------------

CHECKS_YAML = """\
checks:
  - id: net-https-external
    name: Outbound HTTPS
    category: network
    command: curl -fsS --max-time 3 -o /dev/null https://suse.com
    expect: allowed
  - id: net-https-download
    name: openSUSE download
    category: network
    command: curl -fsS -o /dev/null http://download.opensuse.org/tumbleweed/
    expect: allowed
  - id: fs-write-tmp
    name: Write /tmp
    category: filesystem
    command: sh -c 'touch /tmp/.probe && rm -f /tmp/.probe'
    expect: allowed
  - id: fs-read-resolv
    name: Read resolv.conf
    category: filesystem
    command: test -r /etc/resolv.conf
    expect: allowed
  - id: fs-read-shadow
    name: Read shadow
    category: filesystem
    command: head -c1 /etc/shadow
    expect: denied
"""


def test_checks_network_become_network_policies():
    out = checks_to_policy(load_config_text(CHECKS_YAML))
    assert "network_policies:" in out
    assert "suse.com" in out
    assert "download.opensuse.org" in out
    # OpenShell schema: binaries are objects with a path field, not plain strings
    assert "- path: /usr/bin/curl" in out or "- path: /bin/curl" in out


def test_policy_binaries_accept_both_object_and_string_form():
    cfg = policy_to_checks(POLICY)  # POLICY uses string form
    assert any("curl" in c.command for c in cfg.checks if c.category == "network")
    object_form = POLICY.replace("- /usr/bin/curl", "- path: /usr/bin/curl")
    cfg2 = policy_to_checks(object_form)
    assert any("curl" in c.command for c in cfg2.checks if c.category == "network")


def test_checks_fs_grants_become_filesystem_policy():
    out = checks_to_policy(load_config_text(CHECKS_YAML))
    assert "filesystem_policy:" in out
    assert "read_write:" in out and "/tmp" in out
    assert "read_only:" in out and "/etc/resolv.conf" in out


def test_denied_expectations_are_not_granted():
    out = checks_to_policy(load_config_text(CHECKS_YAML))
    assert "/etc/shadow" not in out


def test_policy_starts_with_version():
    out = checks_to_policy(load_config_text(CHECKS_YAML))
    assert out.startswith("version: 1")


def test_roundtrip_preserves_network_hosts():
    cfg1 = policy_to_checks(POLICY)
    out = checks_to_policy(cfg1)
    assert "suse.com" in out
    assert "download.opensuse.org" in out


def load_config_text(text: str):
    from cani.config import parse_config

    return parse_config(text)


def test_fix_for_filesystem_check_needs_recreate():
    from cani.policy import fix_for_check

    check = Check(id="w", name="w", category="filesystem", command="touch /etc/x", expect="denied")
    fix = fix_for_check(check, "can-i")
    assert "- /etc" in fix["yaml"]
    assert "openshell sandbox create --name can-i" in fix["command"]
    assert fix["live"] is False


def test_fix_for_ungrantable_check_explains_why():
    from cani.policy import fix_for_check

    check = Check(id="s", name="s", category="process", command="sh -c 'exit 0'")
    fix = fix_for_check(check, "can-i")
    assert fix["command"] is None and fix["yaml"] is None
    assert "can't express" in fix["note"]


def test_fix_flags_baseline_denials():
    from cani.policy import fix_for_check

    check = Check(id="r", name="r", category="filesystem", command="head -c1 /etc/shadow")
    fix = fix_for_check(check, "can-i", blocker_kind="baseline")
    assert "won't flip it" in fix["note"]


def test_fix_for_ping_explains_icmp_cannot_be_granted():
    from cani.policy import fix_for_check

    check = Check(id="p", name="ping", category="network", command="ping -c1 -W2 one.one.one.one")
    fix = fix_for_check(check, "can-i")
    assert fix["command"] is None
    assert "No OpenShell policy can grant ping" in fix["note"]


def test_fix_for_curl_to_same_host_is_live():
    from cani.policy import fix_for_check

    check = Check(id="c", name="curl", category="network",
                  command="curl -fsS --max-time 3 -o /dev/null https://one.one.one.one")
    fix = fix_for_check(check, "can-i")
    assert "--add-endpoint one.one.one.one:443" in fix["command"]
    assert fix["live"] is True


def test_fix_for_nmap_uses_its_own_rule_and_a_plain_tcp_endpoint():
    from cani.policy import fix_for_check

    check = Check(id="n", name="nmap", category="network",
                  command="sh -c 'nmap -sT -Pn --system-dns -p443 suse.com | grep -q \"443/tcp open\"'")
    fix = fix_for_check(check, "can-i")
    assert re.search(r"--binary \S*/nmap ", fix["command"])
    assert "--rule-name can-i-nmap " in fix["command"]
    assert "--add-endpoint suse.com:443 --wait" in fix["command"]
    assert fix["live"] is True


def test_fix_for_git_uses_its_own_rule_without_read_only():
    from cani.policy import fix_for_check

    check = Check(id="g", name="git", category="network",
                  command="git ls-remote --exit-code https://github.com/wiredquill/can-i HEAD")
    fix = fix_for_check(check, "can-i")
    assert "--rule-name can-i-git " in fix["command"]
    assert "--add-endpoint github.com:443 --wait" in fix["command"]


@pytest.mark.parametrize("command,word", [
    ("curl -fsS -o /dev/null http://169.254.169.254/", "metadata"),
    ("curl -fsS -o /dev/null http://10.0.0.1/", "raw IP"),
    ("unshare --user --map-root-user true", "seccomp"),
])
def test_hard_limits_have_no_fix(command, word):
    from cani.policy import fix_for_check

    cat = "process" if command.startswith("unshare") else "network"
    fix = fix_for_check(Check(id="h", name="h", category=cat, command=command), "can-i")
    assert fix["command"] is None and fix.get("limit") is True
    assert word in fix["note"]


def test_fix_for_var_tmp_write_points_at_the_app_form():
    from cani.policy import fix_for_check

    check = Check(id="w", name="w", category="filesystem",
                  command="sh -c 'touch /var/tmp/.can-i-probe && rm -f /var/tmp/.can-i-probe'")
    fix = fix_for_check(check, "can-i")
    assert "- /var/tmp" in fix["yaml"]
    assert "Extra read-write paths" in fix["note"]
