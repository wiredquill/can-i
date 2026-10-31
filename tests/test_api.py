"""Integration tests for the FastAPI web backend."""
import pytest
from httpx import ASGITransport, AsyncClient

from cani.server import create_app

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
    category: filesystem
    command: 'false'
"""


@pytest.fixture
def config_path(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(YAML)
    return p


@pytest.fixture
async def client(config_path):
    app = create_app(config_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_get_probes_initial_state(client):
    resp = await client.get("/api/probes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert {r["id"] for r in body["results"]} == {"ok", "blocked"}


async def test_post_run_executes_probes(client):
    resp = await client.post("/api/run")
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()["results"]}
    assert by_id["ok"]["status"] == "allowed"
    assert by_id["blocked"]["status"] == "denied"
    assert by_id["blocked"]["exit_code"] == 1


async def test_get_system_info(client):
    resp = await client.get("/api/system")
    assert resp.status_code == 200
    body = resp.json()
    for key in ("hostname", "kernel", "uname", "uid", "user", "capabilities", "sandboxed"):
        assert key in body


async def test_get_config_returns_yaml(client):
    resp = await client.get("/api/config")
    assert resp.status_code == 200
    assert "Always allowed" in resp.text


async def test_put_config_persists_valid_yaml(client, config_path):
    new_yaml = YAML.replace("command: 'false'", "command: 'true'")
    resp = await client.put(
        "/api/config", content=new_yaml, headers={"content-type": "text/plain"}
    )
    assert resp.status_code == 200
    assert "command: 'true'" in config_path.read_text()


async def test_put_config_rejects_invalid_and_keeps_file(client, config_path):
    before = config_path.read_text()
    resp = await client.put(
        "/api/config",
        content="checks:\n  - id: x\n    category: bogus\n",
        headers={"content-type": "text/plain"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]
    assert config_path.read_text() == before


async def test_index_serves_dashboard(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "can-i" in resp.text.lower()


async def test_sse_stream_sends_snapshot_then_probe_events(config_path):
    # SSE is a never-ending stream; httpx's ASGITransport waits for the app to
    # complete, so this must run against a real uvicorn socket.
    import asyncio

    import uvicorn

    app = create_app(config_path)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]

    try:
        async with AsyncClient(base_url=f"http://127.0.0.1:{port}") as live:
            async with asyncio.timeout(15):
                async with live.stream("GET", "/api/events") as resp:
                    assert resp.status_code == 200
                    assert resp.headers["content-type"].startswith("text/event-stream")
                    it = resp.aiter_text()
                    first = await it.__anext__()
                    assert "snapshot" in first
                    # trigger a run; probe events must arrive on the open stream
                    await live.post("/api/run")
                    got_probe = False
                    async for chunk in it:
                        if "probe" in chunk:
                            got_probe = True
                            break
                    assert got_probe
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=10)
