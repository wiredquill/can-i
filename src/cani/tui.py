"""Textual terminal UI for can-i."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static

from .probes import Status
from .state import ProbeState

STATUS_STYLES = {
    Status.ALLOWED: ("ALLOWED", "bold green"),
    Status.DENIED: ("DENIED", "bold red"),
    Status.ERROR: ("ERROR", "bold yellow"),
    Status.RUNNING: ("RUNNING", "bold blue blink"),
}


def status_cell(status: str | Status) -> Text:
    label, style = STATUS_STYLES.get(Status(status), ("?", "white"))
    return Text(label, style=style)


class CanIApp(App):
    """Live sandbox capability dashboard for the terminal."""

    TITLE = "can-i"
    SUB_TITLE = "sandbox capability inspector"

    CSS = """
    #ribbon {
        dock: top;
        height: 3;
        padding: 1 2;
        background: $panel;
        border-bottom: solid $primary;
    }
    DataTable {
        height: 1fr;
    }
    #detail {
        dock: bottom;
        height: 12;
        padding: 1 2;
        border-top: solid $primary;
        background: $panel;
    }
    """

    BINDINGS = [
        ("r", "run_probes", "Run probes"),
        ("space", "run_probes", "Run probes"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, state: ProbeState) -> None:
        super().__init__()
        self.state = state
        self._pump_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="ribbon")
        yield DataTable(cursor_type="row")
        yield Static("select a probe row to inspect stdout/stderr", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Status", "Check", "Category", "Exit", "Latency")
        self.refresh_table()
        self._pump_task = asyncio.create_task(self._event_pump())

    def on_unmount(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()

    # -- rendering --------------------------------------------------------------

    def refresh_table(self) -> None:
        snap = self.state.snapshot()
        sysinfo = snap["system"]
        caps = ", ".join(sysinfo["capabilities"][:5])
        if len(sysinfo["capabilities"]) > 5:
            caps += f" +{len(sysinfo['capabilities']) - 5}…"
        ribbon = Text.assemble(
            (" Host ", "bold reverse"),
            f" {sysinfo['hostname']}  ",
            ("Kernel ", "bold"), f"{sysinfo['kernel']}  ",
            ("User ", "bold"), f"{sysinfo['user']} (uid {sysinfo['uid']})  ",
            ("Container ", "bold"), f"{sysinfo['container_runtime'] or 'host'}  ",
            ("AppArmor ", "bold"), f"{sysinfo['apparmor']}  ",
            ("seccomp ", "bold"), f"{sysinfo['seccomp']}  ",
            ("Sandboxed ", "bold"),
            (str(sysinfo["sandboxed"]), "bold yellow" if sysinfo["sandboxed"] else "green"),
            f"\n Capabilities: {caps or 'none'}",
        )
        self.query_one("#ribbon", Static).update(ribbon)

        table = self.query_one(DataTable)
        cursor_row = table.cursor_row if table.row_count else 0
        table.clear()
        for r in snap["results"]:
            latency = f"{r['latency_ms']:.0f} ms" if r["latency_ms"] else "—"
            table.add_row(
                status_cell(r["status"]),
                r["name"],
                r["category"],
                str(r["exit_code"]) if r["exit_code"] is not None else "—",
                latency,
                key=r["id"],
            )
        if table.row_count:
            table.move_cursor(row=min(cursor_row, table.row_count - 1))

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        result = self.state.results.get(str(event.row_key.value))
        if result is None:
            return
        detail = Text.assemble(
            (f"{result.name} ", "bold"),
            (f"[{result.status.value.upper()}] ", "bold"),
            f"exit code: {result.exit_code if result.exit_code is not None else '—'}  ",
            f"latency: {result.latency_ms:.0f} ms\n",
            ("$ ", "dim"), (result.command, "dim"), "\n",
        )
        if result.expect:
            if result.met is None:
                detail.append(f"expectation: pending (expect {result.expect.upper()})\n", style="dim")
            elif result.met:
                detail.append(f"expectation: ✓ met (expect {result.expect.upper()})\n", style="green")
            else:
                detail.append(f"expectation: ✗ NOT met (expect {result.expect.upper()})\n", style="bold yellow")
        if result.detail:
            detail.append(f"detail: {result.detail}\n", style="yellow")
        if result.stdout:
            detail.append(f"stdout: {result.stdout.strip()[:400]}\n")
        if result.stderr:
            detail.append(f"stderr: {result.stderr.strip()[:400]}", style="red")
        self.query_one("#detail", Static).update(detail)

    # -- actions ------------------------------------------------------------------

    def action_run_probes(self) -> None:
        if not self.state.running:
            self._run_probes()

    @work(exclusive=True)
    async def _run_probes(self) -> None:
        await self.state.run_now()

    async def _event_pump(self) -> None:
        queue = self.state.subscribe()
        try:
            while True:
                await queue.get()
                self.refresh_table()
        finally:
            self.state.unsubscribe(queue)
