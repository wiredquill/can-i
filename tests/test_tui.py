"""Headless tests for the Textual TUI (keybindings, rendering, state updates)."""
import asyncio
import socket

import pytest
from textual.widgets import DataTable

from cani.state import ProbeState
from cani.tui import CanIApp, CategorySwitch, Lane, StageTile

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
    """Rows of every *visible* category box."""
    out = []
    for lane in app.query(Lane):
        if not lane.display:
            continue
        table = lane.query_one(DataTable)
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
        assert "uname -a" in ribbon


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
        table = app.query_one("#table-filesystem", DataTable)
        table.focus()
        table.move_cursor(row=0)
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
        titles = " ".join(str(lane.border_title) for lane in app.query(Lane))
        assert "FILESYSTEM" in titles and "PROCESS" in titles
        assert "exit 1" in text  # 'false' has no recognizable denial signal
        score = str(app.query_one("#score").render())
        assert "by sandbox" in score


async def test_each_category_gets_its_own_box(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        lanes = {lane.category: lane for lane in app.query(Lane)}
        assert set(lanes) == {"filesystem", "process"}  # network absent from this config
        await _run_and_settle(app, state, pilot)
        assert "LOCKED DOWN" in str(lanes["filesystem"].border_subtitle)
        assert "WIDE OPEN" in str(lanes["process"].border_subtitle)


async def test_switch_click_and_show_all(state):
    app = CanIApp(state)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        await pilot.click("#switch-process")
        await pilot.pause()
        assert "Always allowed" not in cell_text(app)
        assert "OFF" in str(app.query_one("#switch-process", CategorySwitch).render())
        await pilot.press("2")  # hide filesystem too: nothing left
        await pilot.pause()
        assert app.query_one("#all-hidden").display
        await pilot.press("0")
        await pilot.pause()
        assert "Always allowed" in cell_text(app) and "Always denied" in cell_text(app)
        assert not app.query_one("#all-hidden").display


async def test_config_reload_adds_a_category_box(state):
    app = CanIApp(state)
    async with app.run_test() as pilot:
        await pilot.pause()
        state.update_config(YAML + """\
  - id: net
    name: Some network probe
    category: network
    command: 'true'
""")
        app.refresh_table()
        for _ in range(10):
            await pilot.pause()
        assert {lane.category for lane in app.query(Lane)} == {"network", "filesystem", "process"}
        assert "Some network probe" in cell_text(app)


def stage_text(app: CanIApp) -> str:
    return "\n".join(str(t.query_one(".st-list").render()) for t in app.query(StageTile) if t.display)


async def test_stage_mode_toggle_shows_big_tiles(state):
    app = CanIApp(state)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        assert not app.query_one("#stage").display
        await pilot.press("s")
        await pilot.pause()
        assert app.query_one("#stage").display
        assert not app.query_one("#lanes").display
        await _run_and_settle(app, state, pilot)
        assert "Always allowed" in stage_text(app) and "Always denied" in stage_text(app)
        assert "CAN THE AGENT" in str(app.query_one("#st-banner").render())
        await pilot.press("2")  # category switches still work in stage mode
        await pilot.pause()
        assert "Always denied" not in stage_text(app)


async def test_stage_mode_announces_revoked_access(state):
    app = CanIApp(state, stage=True)
    async with app.run_test(size=(140, 50)) as pilot:
        await pilot.pause()
        await _run_and_settle(app, state, pilot)
        state.update_config(YAML.replace("command: 'true'", "command: 'false'"))
        await _run_and_settle(app, state, pilot)
        assert "ACCESS REVOKED" in str(app.query_one("#st-banner").render())
        assert "REVOKED" in stage_text(app)
        assert len(app.trend) == 2 and app.trend[-1] < app.trend[0]
