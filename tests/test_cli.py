"""End-to-end tests for the can-i CLI via python -m cani."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

YAML = """\
checks:
  - id: ok
    name: Always allowed
    category: process
    command: 'true'
  - id: blocked
    name: Always denied
    category: filesystem
    command: 'false'
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(YAML)
    return p


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cani", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin", "TERM": "dumb"},
    )


def test_run_prints_table_with_statuses(config_path):
    proc = run_cli("run", "--config", str(config_path))
    assert proc.returncode == 0, proc.stderr
    assert "ALLOWED" in proc.stdout
    assert "DENIED" in proc.stdout
    assert "Always allowed" in proc.stdout


def test_run_json_output_is_machine_readable(config_path):
    proc = run_cli("run", "--config", str(config_path), "--json")
    assert proc.returncode == 0, proc.stderr
    body = json.loads(proc.stdout)
    by_id = {r["id"]: r for r in body["results"]}
    assert by_id["ok"]["status"] == "allowed"
    assert by_id["blocked"]["status"] == "denied"
    assert "system" in body


def test_run_missing_config_fails_cleanly(tmp_path):
    proc = run_cli("run", "--config", str(tmp_path / "nope.yaml"))
    assert proc.returncode != 0
    assert "not found" in (proc.stdout + proc.stderr)


def test_help_lists_subcommands():
    proc = run_cli("--help")
    assert proc.returncode == 0
    for cmd in ("run", "tui", "web"):
        assert cmd in proc.stdout
