"""Tests for the async probe execution engine."""
import asyncio
import time

import pytest

from cani.config import Check
from cani.probes import MAX_OUTPUT_CHARS, ProbeResult, Status, run_all, run_probe


def mkcheck(command: str, timeout: float = 5.0, cid: str = "t1") -> Check:
    return Check(id=cid, name=cid, category="process", command=command, timeout=timeout)


async def test_successful_command_is_allowed():
    r = await run_probe(mkcheck("true"))
    assert r.status == Status.ALLOWED
    assert r.exit_code == 0


async def test_failing_command_is_denied():
    r = await run_probe(mkcheck("false"))
    assert r.status == Status.DENIED
    assert r.exit_code == 1


async def test_exit_code_is_preserved_not_normalized_away():
    r = await run_probe(mkcheck("exit 42"))
    assert r.exit_code == 42
    assert r.status == Status.DENIED


async def test_missing_binary_reports_127_denied():
    r = await run_probe(mkcheck("definitely-not-a-real-binary-xyz"))
    assert r.status == Status.DENIED
    assert r.exit_code == 127


async def test_stdout_and_stderr_captured():
    r = await run_probe(mkcheck("echo out; echo Operation not permitted >&2; exit 1"))
    assert "out" in r.stdout
    assert "Operation not permitted" in r.stderr
    assert r.status == Status.DENIED


async def test_timeout_kills_probe_and_reports_denied():
    start = time.monotonic()
    r = await run_probe(mkcheck("sleep 30", timeout=0.2))
    elapsed = time.monotonic() - start
    assert r.status == Status.DENIED
    assert "timed out" in r.detail.lower()
    assert elapsed < 5  # actually killed, not waited out


async def test_timeout_kills_process_group_not_just_shell():
    # A compound command ("sleep 8; true") prevents the shell from exec-ing the
    # sleep directly: the shell must stay alive, so killing only the shell would
    # leave the sleep holding the pipes open. The engine must kill the group.
    start = time.monotonic()
    r = await run_probe(mkcheck("sleep 8; true", timeout=0.2))
    elapsed = time.monotonic() - start
    assert r.status == Status.DENIED
    assert "timed out" in r.detail.lower()
    assert elapsed < 5


async def test_spawn_failure_is_error_not_denied(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(asyncio, "create_subprocess_shell", boom)
    r = await run_probe(mkcheck("true"))
    assert r.status == Status.ERROR
    assert "exec format error" in r.detail


async def test_latency_is_measured():
    r = await run_probe(mkcheck("sleep 0.1"))
    assert r.latency_ms >= 90


async def test_huge_output_is_truncated():
    r = await run_probe(mkcheck(f"head -c {MAX_OUTPUT_CHARS * 3} /dev/zero | tr '\\0' 'A'"))
    assert len(r.stdout) <= MAX_OUTPUT_CHARS


async def test_run_all_executes_concurrently():
    checks = [mkcheck("sleep 0.3", cid=f"c{i}") for i in range(3)]
    start = time.monotonic()
    results = await run_all(checks)
    elapsed = time.monotonic() - start
    assert len(results) == 3
    assert all(r.status == Status.ALLOWED for r in results)
    assert elapsed < 0.9  # concurrent, not serial


async def test_run_all_emits_live_updates():
    updates: list[ProbeResult] = []

    async def on_update(result: ProbeResult) -> None:
        updates.append(result)

    checks = [mkcheck("true", cid=f"c{i}") for i in range(4)]
    await run_all(checks, on_update=on_update)
    assert len(updates) == 4
    assert {u.id for u in updates} == {f"c{i}" for i in range(4)}


async def test_result_carries_check_metadata():
    r = await run_probe(mkcheck("true", cid="meta-check"))
    assert r.id == "meta-check"
    assert r.name == "meta-check"
    assert r.category == "process"
