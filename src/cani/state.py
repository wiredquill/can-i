"""Shared probe state: drives the TUI, the REST API, and the SSE broadcast."""

from __future__ import annotations

import asyncio
from pathlib import Path

from .attribution import attribute
from .config import Config, load_config, save_config
from .probes import ProbeResult, Status, run_all
from .system_info import gather


class ProbeState:
    """Holds config, latest results, and live event subscribers."""

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path)
        self.config: Config = load_config(self.config_path)
        self.results: dict[str, ProbeResult] = {
            c.id: ProbeResult(
                id=c.id, name=c.name, category=c.category, command=c.command,
                expect=c.expect, status=Status.RUNNING,
            )
            for c in self.config.checks
        }
        self.running = False
        self._run_lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()

    # -- subscription fan-out -------------------------------------------------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    async def _broadcast(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass  # slow consumer drops events rather than blocking probes

    # -- snapshot --------------------------------------------------------------

    def snapshot(self) -> dict:
        ordered = [self.results[c.id].model_dump(mode="json") for c in self.config.checks]
        return {
            "running": self.running,
            "interval_seconds": self.config.settings.interval_seconds,
            "results": ordered,
            "system": gather().model_dump(mode="json"),
        }

    # -- execution -------------------------------------------------------------

    async def run_now(self) -> list[ProbeResult]:
        async with self._run_lock:
            self.running = True
            await self._broadcast({"type": "run_start"})
            system = gather()

            async def on_update(result: ProbeResult) -> None:
                result.blocker = attribute(result, system)
                self.results[result.id] = result
                await self._broadcast(
                    {"type": "probe", "data": result.model_dump(mode="json")}
                )

            try:
                results = await run_all(self.config.checks, on_update=on_update)
            finally:
                self.running = False
            await self._broadcast({"type": "run_complete"})
            return results

    # -- live configuration ----------------------------------------------------

    def update_config(self, text: str) -> Config:
        """Validate, persist, and hot-reload a new checks.yaml.

        When the config file can't be written (the image's /etc/can-i is
        read-only inside an OpenShell sandbox), the edit is saved to
        ~/.config/can-i/checks.yaml instead, which becomes the live config.
        """
        try:
            cfg = save_config(self.config_path, text)  # raises before touching state
        except OSError:
            override = Path.home() / ".config" / "can-i" / "checks.yaml"
            if self.config_path == override:
                raise
            override.parent.mkdir(parents=True, exist_ok=True)
            cfg = save_config(override, text)
            self.config_path = override
        self.config = cfg
        known = {c.id for c in cfg.checks}
        self.results = {k: v for k, v in self.results.items() if k in known}
        for c in cfg.checks:
            old = self.results.get(c.id)
            # a changed check starts over, so the old result isn't shown against the new command
            if old is None or (old.name, old.category, old.command, old.expect) != (
                c.name, c.category, c.command, c.expect
            ):
                self.results[c.id] = ProbeResult(
                    id=c.id, name=c.name, category=c.category, command=c.command,
                    expect=c.expect, status=Status.RUNNING,
                )
        return cfg
