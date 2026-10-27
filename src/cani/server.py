"""FastAPI backend: REST endpoints, SSE broadcast, and the web dashboard."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse

from .config import ConfigError
from .state import ProbeState

WEB_DIR = Path(__file__).parent / "web"


def _sandbox_name() -> str:
    """This sandbox's OpenShell name: its hostname is <workspace>--<name>."""
    host = socket.gethostname()
    return host.split("--", 1)[1] if "--" in host else "can-i"


def create_app(config_path: str | Path) -> FastAPI:
    state = ProbeState(config_path)
    app = FastAPI(title="can-i", version="0.1.0")
    app.state.probe_state = state

    @app.get("/api/system")
    async def get_system() -> dict:
        return state.snapshot()["system"]

    @app.get("/api/probes")
    async def get_probes() -> dict:
        snap = state.snapshot()
        return {"running": snap["running"], "results": snap["results"]}

    @app.post("/api/run")
    async def post_run() -> dict:
        results = await state.run_now()
        return {"results": [r.model_dump(mode="json") for r in results]}

    @app.get("/api/config", response_class=PlainTextResponse)
    async def get_config() -> str:
        return state.config_path.read_text(encoding="utf-8")

    @app.put("/api/config", response_class=PlainTextResponse)
    async def put_config(request: Request) -> str:
        text = (await request.body()).decode("utf-8")
        try:
            state.update_config(text)
        except ConfigError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"could not save checks.yaml: {exc}") from exc
        await state._broadcast({"type": "config"})
        return text

    @app.post("/api/policy/import", response_class=PlainTextResponse)
    async def import_policy(request: Request) -> str:
        """Turn an OpenShell policy document into checks.yaml text."""
        from .policy import policy_to_checks

        text = (await request.body()).decode("utf-8")
        try:
            cfg = policy_to_checks(text)
        except (ValueError, ConfigError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return cfg.to_yaml()

    @app.get("/api/policy/export", response_class=PlainTextResponse)
    async def export_policy() -> str:
        """Generate an OpenShell policy from the current checks."""
        from .policy import checks_to_policy

        return checks_to_policy(state.config)

    @app.get("/api/fix/{check_id}")
    async def get_fix(check_id: str) -> dict:
        """How to change the policy so this check becomes ALLOWED."""
        from .policy import fix_for_check

        check = next((c for c in state.config.checks if c.id == check_id), None)
        if check is None:
            raise HTTPException(status_code=404, detail=f"no check {check_id!r}")
        result = state.results.get(check_id)
        kind = result.blocker.kind if result and result.blocker else None
        sandbox = os.environ.get("CANI_SANDBOX_NAME") or _sandbox_name()
        return {"sandbox": sandbox, **fix_for_check(check, sandbox, kind)}

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = state.subscribe()

        async def stream():
            try:
                yield f"event: snapshot\ndata: {json.dumps(state.snapshot())}\n\n"
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield f"event: {event['type']}\ndata: {json.dumps(event.get('data', {}))}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                state.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return (WEB_DIR / "index.html").read_text(encoding="utf-8")

    return app
