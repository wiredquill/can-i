"""Tests for OpenShell policy <-> checks.yaml interop."""
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
    assert "/usr/bin/curl" in out or "/bin/curl" in out


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
