"""Async probe execution engine.

A probe runs one benign shell command. Exit code 0 means the action is
ALLOWED by the current sandbox/policy; anything else means DENIED.
"""

from __future__ import annotations

import asyncio
import enum
import time
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from .config import Check

MAX_OUTPUT_CHARS = 4096


class Status(str, enum.Enum):
    RUNNING = "running"
    ALLOWED = "allowed"
    DENIED = "denied"
    ERROR = "error"  # the probe itself failed to execute (spawn error)


class ProbeResult(BaseModel):
    id: str
    name: str
    category: str
    command: str = ""
    expect: str | None = None
    met: bool | None = None  # None when the check carries no expectation
    status: Status
    exit_code: int | None = None
    latency_ms: float = 0.0
    stdout: str = ""
    stderr: str = ""
    detail: str = ""


def _truncate(text: str) -> str:
    marker = "\n… [truncated]"
    if len(text) > MAX_OUTPUT_CHARS:
        return text[: MAX_OUTPUT_CHARS - len(marker)] + marker
    return text


def _met(status: Status, expect: str | None) -> bool | None:
    if expect is None:
        return None
    return (status == Status.ALLOWED) == (expect == "allowed")


async def run_probe(check: Check) -> ProbeResult:
    """Execute a single check with timeout enforcement and output capture."""
    timeout = check.timeout or 5.0
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            check.command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return ProbeResult(
            id=check.id,
            name=check.name,
            category=check.category,
            command=check.command,
            expect=check.expect,
            met=_met(Status.ERROR, check.expect),
            status=Status.ERROR,
            latency_ms=(time.monotonic() - start) * 1000,
            detail=str(exc),
        )

    timed_out = False
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        proc.kill()
        stdout_b, stderr_b = await proc.communicate()

    latency_ms = (time.monotonic() - start) * 1000
    stdout = _truncate(stdout_b.decode(errors="replace"))
    stderr = _truncate(stderr_b.decode(errors="replace"))

    if timed_out:
        return ProbeResult(
            id=check.id,
            name=check.name,
            category=check.category,
            command=check.command,
            expect=check.expect,
            met=_met(Status.DENIED, check.expect),
            status=Status.DENIED,
            exit_code=proc.returncode,
            latency_ms=latency_ms,
            stdout=stdout,
            stderr=stderr,
            detail=f"timed out after {timeout}s",
        )

    exit_code = proc.returncode if proc.returncode is not None else -1
    return ProbeResult(
        id=check.id,
        name=check.name,
        category=check.category,
        command=check.command,
        expect=check.expect,
        status=Status.ALLOWED if exit_code == 0 else Status.DENIED,
        met=_met(Status.ALLOWED if exit_code == 0 else Status.DENIED, check.expect),
        exit_code=exit_code,
        latency_ms=latency_ms,
        stdout=stdout,
        stderr=stderr,
    )


async def run_all(
    checks: list[Check],
    on_update: Callable[[ProbeResult], Awaitable[None]] | None = None,
) -> list[ProbeResult]:
    """Run all checks concurrently, optionally emitting each result live."""

    async def _run(check: Check) -> ProbeResult:
        result = await run_probe(check)
        if on_update is not None:
            await on_update(result)
        return result

    return list(await asyncio.gather(*(_run(c) for c in checks)))
