"""Tests for the shared probe state (drives TUI, API, and SSE broadcast)."""
import asyncio

import pytest

from cani.probes import Status
from cani.state import ProbeState

YAML = """\
settings:
  interval_seconds: 3
checks:
  - id: ok
    name: Always allowed
    category: process
    command: 'true'
  - id: blocked
    name: Always denied
    category: process
    command: 'false'
"""


@pytest.fixture
def state(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(YAML)
    return ProbeState(p)


async def test_initial_results_are_pending(state):
    snap = state.snapshot()
    assert snap["running"] is False
    assert {r["id"] for r in snap["results"]} == {"ok", "blocked"}
    assert all(r["status"] == "running" for r in snap["results"])


async def test_run_now_populates_results(state):
    results = await state.run_now()
    by_id = {r.id: r for r in results}
    assert by_id["ok"].status == Status.ALLOWED
    assert by_id["blocked"].status == Status.DENIED
    snap = state.snapshot()
    assert {r["status"] for r in snap["results"]} == {"allowed", "denied"}


async def test_subscribers_receive_probe_events(state):
    q = state.subscribe()
    await state.run_now()
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    state.unsubscribe(q)
    kinds = [e["type"] for e in events]
    assert kinds.count("probe") == 2
    assert "run_start" in kinds
    assert "run_complete" in kinds


async def test_concurrent_runs_are_serialized(state):
    # Second run waits for the first; no overlapping probe executions.
    r1, r2 = await asyncio.gather(state.run_now(), state.run_now())
    assert len(r1) == len(r2) == 2


async def test_update_config_reloads_checks(state, tmp_path):
    new_yaml = YAML.replace("command: 'false'", "command: 'true'")
    cfg = state.update_config(new_yaml)
    assert state.config.checks[1].command == "true"
    results = await state.run_now()
    assert all(r.status == Status.ALLOWED for r in results)


async def test_update_config_invalid_raises_and_keeps_old(state):
    from cani.config import ConfigError

    with pytest.raises(ConfigError):
        state.update_config("checks: []\n")
    assert len(state.config.checks) == 2


async def test_system_info_in_snapshot(state):
    snap = state.snapshot()
    assert "system" in snap
    assert snap["system"]["hostname"]


async def test_update_config_resets_changed_checks(state):
    await state.run_now()
    state.update_config(YAML.replace("command: 'false'", "command: 'exit 3'"))
    assert state.results["blocked"].command == "exit 3"
    assert state.results["blocked"].status == Status.RUNNING
    assert state.results["ok"].status == Status.ALLOWED  # unchanged check keeps its result


async def test_update_config_falls_back_to_home_when_read_only(tmp_path, monkeypatch):
    etc = tmp_path / "etc"  # like /etc/can-i in the sandbox
    etc.mkdir()
    (etc / "checks.yaml").write_text(YAML)
    state = ProbeState(etc / "checks.yaml")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    etc.chmod(0o555)
    try:
        state.update_config(YAML.replace("command: 'false'", "command: 'true'"))
    finally:
        etc.chmod(0o755)
    override = home / ".config" / "can-i" / "checks.yaml"
    assert state.config_path == override
    assert "command: 'true'" in override.read_text()
    assert state.config.checks[1].command == "true"
