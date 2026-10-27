"""CLI + API surface for OpenShell policy interop."""
import subprocess
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from cani.server import create_app

SRC = Path(__file__).resolve().parent.parent / "src"

POLICY = """\
version: 1
filesystem_policy:
  read_write:
    - /tmp
network_policies:
  allow-suse:
    name: allow-suse
    endpoints:
      - host: suse.com
        port: 443
    binaries:
      - /usr/bin/curl
"""

CHECKS_YAML = """\
checks:
  - id: net-https-external
    name: Outbound HTTPS
    category: network
    command: curl -fsS -o /dev/null https://suse.com
    expect: allowed
"""


def run_cli(*args: str, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cani", *args],
        input=stdin, capture_output=True, text=True, timeout=60,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin", "TERM": "dumb"},
    )


# ---- CLI ---------------------------------------------------------------------


def test_cli_from_policy_file(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(POLICY)
    proc = run_cli("from-policy", str(p))
    assert proc.returncode == 0, proc.stderr
    assert "net-allow-suse-com" in proc.stdout
    assert "expect: allowed" in proc.stdout
    assert "expect: denied" in proc.stdout  # control probes


def test_cli_from_policy_writes_output(tmp_path):
    p = tmp_path / "policy.yaml"
    p.write_text(POLICY)
    out = tmp_path / "checks.yaml"
    proc = run_cli("from-policy", str(p), "--output", str(out))
    assert proc.returncode == 0
    assert "net-allow-suse-com" in out.read_text()


def test_cli_to_policy(tmp_path):
    c = tmp_path / "checks.yaml"
    c.write_text(CHECKS_YAML)
    proc = run_cli("to-policy", "--config", str(c))
    assert proc.returncode == 0, proc.stderr
    assert "version: 1" in proc.stdout
    assert "suse.com" in proc.stdout
    assert "network_policies:" in proc.stdout


# ---- API ---------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(CHECKS_YAML)
    app = create_app(p)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_api_import_policy_returns_generated_checks(client):
    resp = await client.post(
        "/api/policy/import", content=POLICY, headers={"content-type": "text/plain"}
    )
    assert resp.status_code == 200
    assert "net-allow-suse-com" in resp.text


async def test_api_import_policy_rejects_garbage(client):
    resp = await client.post(
        "/api/policy/import", content="version: 1\n", headers={"content-type": "text/plain"}
    )
    assert resp.status_code == 422


async def test_api_export_policy(client):
    resp = await client.get("/api/policy/export")
    assert resp.status_code == 200
    assert "suse.com" in resp.text
    assert resp.text.startswith("version: 1")


async def test_api_fix_for_network_check(client, monkeypatch):
    monkeypatch.setenv("CANI_SANDBOX_NAME", "demo")
    resp = await client.get("/api/fix/net-https-external")
    assert resp.status_code == 200
    fix = resp.json()
    assert fix["sandbox"] == "demo"
    assert fix["command"].startswith("openshell policy update demo")
    assert "--add-endpoint suse.com:443:read-only:rest:enforce" in fix["command"]
    assert "host: suse.com" in fix["yaml"]
    assert fix["live"] is True


async def test_api_fix_unknown_check(client):
    resp = await client.get("/api/fix/nope")
    assert resp.status_code == 404


def test_sandbox_name_comes_from_the_openshell_hostname(monkeypatch):
    from cani import server

    monkeypatch.setattr(server.socket, "gethostname", lambda: "default--can-i-sandbox")
    assert server._sandbox_name() == "can-i-sandbox"
    monkeypatch.setattr(server.socket, "gethostname", lambda: "laptop")
    assert server._sandbox_name() == "can-i"
