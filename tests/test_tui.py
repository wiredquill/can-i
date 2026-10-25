"""Headless tests for the Textual TUI (keybindings, rendering, state updates)."""
import asyncio
import socket

import pytest
from textual.widgets import DataTable

from cani.state import ProbeState
from cani.tui import CanIApp

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
def state(tmp_path):
    p = tmp_path / "checks.yaml"
    p.write_text(YAML)
    return ProbeState(p)


def cell_text(app: CanIApp) -> str:
    table = app.query_one(DataTable)
    out = []
    for row_key in table.rows:
        out.append(" ".join(str(c) for c in table.get_row(row_key)))
    return "\n".join(out)


async def test_initial_render_shows_checks_as_running(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        text = cell_text(app)
        assert "Always allowed" in text
        assert "Always denied" in text
        assert text.count("RUNNING") == 2


async def test_ribbon_shows_system_summary(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        ribbon = str(app.query_one("#ribbon").render())
        assert socket.gethostname() in ribbon
        assert "uid" in ribbon.lower() or "user" in ribbon.lower()


async def test_run_keybinding_updates_statuses(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("r")
        for _ in range(100):
            await pilot.pause()
            if not state.running and "ALLOWED" in cell_text(app):
                break
        text = cell_text(app)
        assert "ALLOWED" in text
        assert "DENIED" in text


async def test_space_also_triggers_run(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")
        for _ in range(100):
            await pilot.pause()
            if "ALLOWED" in cell_text(app):
                break
        assert "ALLOWED" in cell_text(app)


async def test_quit_binding_exits(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("q")
    assert app._exit  # app exited


async def test_row_selection_shows_detail_panel(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await state.run_now()
        await pilot.pause()
        app.refresh_table()
        table = app.query_one(DataTable)
        table.move_cursor(row=1)
        await pilot.pause()
        detail = str(app.query_one("#detail").render())
        assert "Always denied" in detail
        assert "exit" in detail.lower()


async def _run_and_settle(app, state, pilot):
    await state.run_now()
    for _ in range(20):
        await pilot.pause()


async def test_flip_shows_lost_delta_and_notifies(state, tmp_path):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_and_settle(app, state, pilot)
        assert "LOST" not in cell_text(app)
        # policy change: the always-allowed probe now fails
        state.update_config(YAML.replace("command: 'true'", "command: 'false'"))
        await _run_and_settle(app, state, pilot)
        assert "▼ LOST" in cell_text(app)
        assert any("LOCKED DOWN" in str(n.message) for n in app._notifications)


async def test_baseline_keeps_delta_across_runs(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_and_settle(app, state, pilot)
        await pilot.press("b")
        state.update_config(YAML.replace("command: 'true'", "command: 'false'"))
        await _run_and_settle(app, state, pilot)
        await _run_and_settle(app, state, pilot)  # second run: still lost vs baseline
        assert "▼ LOST" in cell_text(app)
        await pilot.press("b")  # clear: last run had the same verdict, so no delta
        await pilot.pause()
        assert "LOST" not in cell_text(app)


async def test_category_filter_hides_rows(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("2")  # filesystem
        await pilot.pause()
        assert "Always denied" not in cell_text(app)
        assert "Always allowed" in cell_text(app)
        await pilot.press("2")
        await pilot.pause()
        assert "Always denied" in cell_text(app)


async def test_category_and_blocker_columns(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        await _run_and_settle(app, state, pilot)
        text = cell_text(app)
        assert "FILESYSTEM" in text and "PROCESS" in text
        assert "exit 1" in text  # 'false' has no recognizable denial signal
        score = str(app.query_one("#score").render())
        assert "by sandbox" in score
