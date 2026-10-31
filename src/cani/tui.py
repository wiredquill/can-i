"""Textual terminal UI for can-i."""

from __future__ import annotations

import asyncio

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Digits, Footer, Header, Sparkline, Static

from .probes import Blocker, ProbeResult, Status
from .state import ProbeState

GREEN, RED, AMBER, BLUE, SANDBOX = "#2fbf71", "#e5484d", "#f5a524", "#4c9aff", "#f472b6"

STATUS_STYLES = {
    Status.ALLOWED: ("✔ ALLOWED", f"bold {GREEN}"),
    Status.DENIED: ("✘ DENIED", f"bold {RED}"),
    Status.ERROR: ("⚠ ERROR", f"bold {AMBER}"),
    Status.RUNNING: ("… RUNNING", f"bold {BLUE} blink"),
}

# category -> (glyph, label, color); mirrors the web dashboard's lanes
CATEGORIES = {
    "network": ("◉", "NETWORK", "#4c9aff"),
    "filesystem": ("▤", "FILESYSTEM", "#bc8cff"),
    "process": ("⚙", "PROCESS", "#22d3ee"),
}
CATEGORY_SUBS = {
    "network": "egress · DNS · sockets",
    "filesystem": "read · write · exec",
    "process": "spawn · namespaces · privesc",
}
COLUMNS = ("Status", "Δ", "Check", "Blocked by", "Exit", "Latency", "History")


def cat_meta(category: str) -> tuple[str, str, str]:
    return CATEGORIES.get(category, ("•", category.upper(), "#8ba0b8"))

BLOCKER_STYLES = {
    "sandbox": ("◆", f"bold {SANDBOX}"),  # the sandbox policy said no
    "baseline": ("·", "dim"),  # would be denied without a sandbox too
    "unknown": ("?", "dim italic"),
}

HISTORY = 10
FLASH_SECONDS = 2.0
BANNER_SECONDS = 8.0
TREND = 60  # runs kept for the stage-mode trend line


def granted(status: str) -> bool:
    return status == Status.ALLOWED.value


def status_cell(status: str | Status) -> Text:
    label, style = STATUS_STYLES.get(Status(status), ("?", "white"))
    return Text(label, style=style)


def category_cell(category: str) -> Text:
    glyph, label, color = cat_meta(category)
    return Text(f"{glyph} {label}", style=f"bold {color}")


def blocker_cell(blocker: Blocker | None) -> Text:
    if blocker is None:
        return Text("")
    glyph, style = BLOCKER_STYLES[blocker.kind]
    return Text(f"{glyph} {blocker.layer}", style=style)


def delta_cell(kind: str | None) -> Text:
    if kind == "gained":
        return Text(" ▲ GAINED ", style=f"bold #04140a on {GREEN}")
    if kind == "lost":
        return Text(" ▼ LOST ", style=f"bold white on {RED}")
    return Text("")


def history_cell(statuses: list[str]) -> Text:
    colors = {"allowed": GREEN, "denied": RED, "error": AMBER}
    text = Text()
    for s in statuses[-HISTORY:]:
        text.append("■", style=colors.get(s, "dim"))
    return text


def meter(allowed: int, total: int, width: int = 10) -> Text:
    filled = round(width * allowed / total) if total else 0
    return Text.assemble(("█" * filled, GREEN), ("█" * (width - filled), RED if total else "dim"))


class CategorySwitch(Static):
    """Clickable on/off tile for one category's view (keys 1-9 do the same)."""

    def __init__(self, category: str, key: int) -> None:
        super().__init__(id=f"switch-{category}", classes="switch")
        self.category = category
        self.key = key
        self.styles.border = ("round", cat_meta(category)[2])

    def show(self, on: bool, allowed: int, total: int) -> None:
        glyph, label, color = cat_meta(self.category)
        self.set_class(not on, "off")
        self.styles.border = ("round", color if on else "#3a4656")
        head = Text.assemble((f"[{self.key}] ", "dim"),
                             (f"{glyph} {label}", f"bold {color}" if on else "dim strike"))
        state = Text(" ● ON " if on else " ○ OFF ",
                     style=f"bold #04140a on {color}" if on else "bold #8ba0b8 on #1b2533")
        bar = meter(allowed, total) if on else Text("·" * 10, style="dim")
        body = Text.assemble(bar, (f" {allowed}/{total} allowed", "dim"))
        self.update(Text("\n").join([Text.assemble(head, "  ", state), body]))

    def on_click(self) -> None:
        self.app.action_toggle_cat(self.category)


class Lane(Vertical):
    """A boxed group of probes sharing one category."""

    def __init__(self, category: str) -> None:
        super().__init__(id=f"lane-{category}", classes="lane")
        self.category = category
        self.styles.border = ("heavy", cat_meta(category)[2])

    def compose(self) -> ComposeResult:
        table = DataTable(cursor_type="row", id=f"table-{self.category}")
        table.add_columns(*COLUMNS)
        yield table

    @property
    def table(self) -> DataTable:
        return self.query_one(DataTable)

    def summarize(self, allowed: int, denied: int, by_sandbox: int, total: int) -> None:
        glyph, label, color = cat_meta(self.category)
        sub = CATEGORY_SUBS.get(self.category, "")
        self.border_title = f"[b {color}] {glyph} {label} [/][dim]{sub} [/]"
        verdict = ("LOCKED DOWN", RED) if total and denied == total else \
                  ("WIDE OPEN", GREEN) if total and allowed == total else ("PARTIAL", AMBER)
        sandbox = f"[{SANDBOX}]◆ {by_sandbox} by sandbox[/] · " if by_sandbox else ""
        self.border_subtitle = (f" {sandbox}[{GREEN}]{allowed} allowed[/] · [{RED}]{denied} denied[/]"
                                f"  [b {verdict[1]}]{verdict[0]}[/] ")

    def flash(self, kind: str) -> None:
        self.styles.border = ("heavy", RED if kind == "lost" else GREEN)
        self.add_class(f"flash-{kind}")

        def done() -> None:
            self.styles.border = ("heavy", cat_meta(self.category)[2])
            self.remove_class(f"flash-{kind}")

        self.set_timer(FLASH_SECONDS, done)


class StageTile(Vertical):
    """Stage mode: one big tile per category, readable from the back of a room."""

    def __init__(self, category: str) -> None:
        super().__init__(id=f"stage-{category}", classes="stage-tile")
        self.category = category
        self.styles.border = ("heavy", cat_meta(category)[2])

    def compose(self) -> ComposeResult:
        yield Digits("–", classes="st-digits")
        yield Static(classes="st-sub")
        yield Static(classes="st-list")

    def show(self, rows: list[tuple[dict, str | None, str | None, Blocker | None, str | None]]) -> None:
        """rows: (result, verdict, delta, blocker, flash) per probe in this category."""
        glyph, label, color = cat_meta(self.category)
        self.border_title = f"[b {color}] {glyph}  {label} [/]"
        allowed = sum(1 for _, v, *_ in rows if v and granted(v))
        ran = sum(1 for _, v, *_ in rows if v)
        digits = self.query_one(Digits)
        digits.update(str(allowed) if ran else "–")
        digits.styles.color = GREEN if allowed else RED if ran else "#5a6b80"
        verdict = ("NOT RUN", "#5a6b80") if not ran else ("WIDE OPEN", GREEN) if allowed == len(rows) else \
                  ("LOCKED DOWN", RED) if allowed == 0 else ("PARTIAL", AMBER)
        # short screens hide the big digit, so the count moves into this line
        count = f"{allowed} of {len(rows)}" if self.screen.has_class("-short") and ran else f"of {len(rows)}"
        self.query_one(".st-sub", Static).update(Text.assemble(
            (f"{count} actions allowed  ", "dim"), (f" {verdict[0]} ", f"bold #05080d on {verdict[1]}")))
        lines = []
        for r, v, delta, blocker, flash in rows:
            # two lines per probe so nothing wraps: the action, then why / what changed
            if v is None:
                line = Text.assemble(("  …  ", "dim"), (r["name"], "dim"))
            elif granted(v):
                line = Text.assemble(("  ✔  ", f"bold {GREEN}"), (r["name"], "bold"))
            else:
                line = Text.assemble(("  ✘  ", f"bold {RED}"), (r["name"], "bold #c9d3de"))
            if flash:
                line.stylize("on #123a24" if flash == "gained" else "on #4a1517")
            sub = Text("     ")
            if delta == "gained":
                sub.append(" ▲ GRANTED ", style=f"bold #04140a on {GREEN}")
                sub.append(" ")
            elif delta == "lost":
                sub.append(" ▼ REVOKED ", style=f"bold white on {RED}")
                sub.append(" ")
            if blocker is not None:
                bglyph, bstyle = BLOCKER_STYLES[blocker.kind]
                sub.append(f"{bglyph} {blocker.layer}", style=bstyle)
            lines.append(Text("\n").join([line, sub]))
        self.query_one(".st-list", Static).update(Text("\n\n").join(lines))


class CanIApp(App):
    """Live sandbox capability dashboard for the terminal."""

    TITLE = "can-i"
    SUB_TITLE = "sandbox capability inspector"

    CSS = """
    #ribbon {
        height: auto;
        padding: 0 2;
        background: $panel;
        border-bottom: solid $primary;
    }
    #score {
        height: auto;
        padding: 0 2;
        background: $boost;
    }
    #switches {
        height: auto;
        padding: 0 1;
    }
    .switch {
        width: 1fr;
        height: auto;
        padding: 0 1;
        margin: 0 1;
    }
    .switch.off {
        color: $text-muted;
    }
    #lanes {
        height: 1fr;
        padding: 0 1;
    }
    .lane {
        height: auto;
        margin: 1 1 0 1;
        padding: 0 1;
        border-title-align: left;
        border-subtitle-align: right;
    }
    .lane.flash-lost {
        background: #e5484d 12%;
    }
    .lane.flash-gained {
        background: #2fbf71 12%;
    }
    .lane DataTable {
        height: auto;
        max-height: 14;
    }
    /* only the box you're in shows a cursor row */
    .lane DataTable:blur > .datatable--cursor {
        background: transparent;
        text-style: none;
    }
    .lane:focus-within {
        background: $boost;
    }
    #all-hidden {
        padding: 2 4;
        color: $text-muted;
        display: none;
    }
    #stage {
        height: 1fr;
        display: none;
    }
    #st-banner {
        height: 3;
        content-align: center middle;
        text-style: bold;
        background: $panel;
    }
    #st-score {
        height: auto;
        padding: 0 1;
    }
    .st-box {
        width: 1fr;
        height: auto;
        margin: 0 1;
        border: round $primary-darken-2;
        align-horizontal: center;
    }
    .st-box Digits {
        width: auto;
        text-style: bold;
    }
    .st-box Static {
        width: auto;
        color: $text-muted;
    }
    #st-tiles {
        height: 1fr;
        padding: 0 1;
    }
    .stage-tile {
        width: 1fr;
        height: 1fr;
        overflow-y: auto;
        margin: 1 1 0 1;
        padding: 0 2;
        border-title-align: center;
    }
    .stage-tile Digits {
        width: 100%;
        text-align: center;
        text-style: bold;
    }
    .st-list {
        text-wrap: nowrap;
        text-overflow: ellipsis;
    }
    .st-sub {
        width: 100%;
        text-align: center;
        margin-bottom: 1;
    }
    #st-trend {
        height: 5;
        margin: 1 2 0 2;
        padding: 0 1;
        border: round $primary-darken-2;
        border-title-align: left;
    }
    #st-trend Sparkline {
        height: 3;
    }
    #st-trend Sparkline > .sparkline--max-color {
        color: #2fbf71;
    }
    #st-trend Sparkline > .sparkline--min-color {
        color: #e5484d;
    }
    /* short terminals (kubectl exec windows): give the category tiles all the room */
    Screen.-short #st-score, Screen.-short #st-trend {
        display: none;
    }
    Screen.-short .stage-tile Digits {
        display: none;
    }
    CanIApp.stage #ribbon, CanIApp.stage #score, CanIApp.stage #lanes, CanIApp.stage #detail {
        display: none;
    }
    CanIApp.stage #stage {
        display: block;
    }
    #detail {
        height: 12;
        padding: 1 2;
        border-top: solid $primary;
        background: $panel;
    }
    """

    VERTICAL_BREAKPOINTS = [(0, "-short"), (40, "-tall")]

    BINDINGS = [
        ("r", "run_probes", "Run probes"),
        ("space", "run_probes", "Run probes"),
        ("a", "toggle_auto", "Auto"),
        ("b", "toggle_baseline", "Pin baseline"),
        ("1", "toggle_cat_key(1)", "Network"),
        ("2", "toggle_cat_key(2)", "Files"),
        ("3", "toggle_cat_key(3)", "Process"),
        *[(str(n), f"toggle_cat_key({n})", "") for n in range(4, 10)],
        ("0", "show_all", "Show all"),
        ("s", "toggle_stage", "Stage mode"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, state: ProbeState, stage: bool = False) -> None:
        super().__init__()
        self.state = state
        self.stage = stage
        self.trend: list[int] = []  # allowed count after each run
        self._banner_timer = None
        self._pump_task: asyncio.Task | None = None
        # access-change tracking, all keyed by probe id with status strings
        self.last: dict[str, str] = {}  # latest final verdict
        self.prev: dict[str, str | None] = {}  # verdict before the latest one
        self.hist: dict[str, list[str]] = {}
        self.baseline: dict[str, str] | None = None
        self.flashing: dict[str, str] = {}  # id -> "gained"/"lost" while the row flashes
        self.run_flips: list[tuple[str, ProbeResult]] = []
        self.updated: set[str] = set()  # probes that reported during the current run
        self.hidden: set[str] = set()
        self.auto = False
        self.cats: list[str] = self._categories()

    def _categories(self) -> list[str]:
        present = {c.category for c in self.state.config.checks}
        return [c for c in CATEGORIES if c in present] + sorted(present - CATEGORIES.keys())

    def cat_key(self, category: str) -> int:
        """Stable number key: built-ins keep 1-3 whatever the config contains."""
        extras = sorted({c.category for c in self.state.config.checks} - CATEGORIES.keys())
        return ([*CATEGORIES, *extras].index(category) + 1)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="ribbon")
        yield Static(id="score")
        with Horizontal(id="switches"):
            for cat in self.cats:
                yield CategorySwitch(cat, self.cat_key(cat))
        with VerticalScroll(id="lanes"):
            for cat in self.cats:
                yield Lane(cat)
            yield Static("all categories hidden — press [b]0[/] to show everything, or 1-3 to bring one back",
                         id="all-hidden")
        with Vertical(id="stage"):
            yield Static(id="st-banner")
            with Horizontal(id="st-score"):
                for key, label, color in (("allowed", "ALLOWED", GREEN), ("denied", "DENIED", RED),
                                          ("granted", "GRANTED", GREEN), ("revoked", "REVOKED", RED)):
                    with Vertical(classes="st-box"):
                        digits = Digits("–", id=f"st-{key}")
                        digits.styles.color = color
                        yield digits
                        yield Static(label, id=f"st-{key}-label")
            with Horizontal(id="st-tiles"):
                for cat in self.cats:
                    yield StageTile(cat)
            with Vertical(id="st-trend"):
                yield Sparkline([0, 0], summary_function=max)
        yield Static("select a probe row to inspect stdout/stderr", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        self.set_class(self.stage, "stage")
        self.query_one("#st-trend").border_title = "access over time (allowed per run)"
        self._banner()
        for result in self.state.results.values():
            self._ingest(result, live=False)
        self.refresh_table()
        lanes = self.query(Lane)
        if lanes:
            lanes.first().table.focus()
        self._pump_task = asyncio.create_task(self._event_pump())

    async def _rebuild_lanes(self) -> None:
        """Config hot-reload changed the set of categories: rebuild boxes and switches."""
        self.cats = self._categories()
        self.hidden &= set(self.cats)
        switches, lanes = self.query_one("#switches"), self.query_one("#lanes")
        await switches.remove_children()
        await switches.mount_all([CategorySwitch(c, self.cat_key(c)) for c in self.cats])
        await lanes.remove_children(".lane")
        await lanes.mount_all([Lane(c) for c in self.cats], before="#all-hidden")
        tiles = self.query_one("#st-tiles")
        await tiles.remove_children()
        await tiles.mount_all([StageTile(c) for c in self.cats])
        self.refresh_table()

    def on_unmount(self) -> None:
        if self._pump_task:
            self._pump_task.cancel()

    # -- access-change tracking -------------------------------------------------

    def _ingest(self, result: ProbeResult, live: bool) -> None:
        if result.status == Status.RUNNING:
            return
        status = result.status.value
        before = self.last.get(result.id)
        self.prev[result.id] = before
        if live and before is not None and granted(before) != granted(status):
            kind = "gained" if granted(status) else "lost"
            self.run_flips.append((kind, result))
            self._flash(result.id, kind)
            for lane in self.query(Lane):
                if lane.category == result.category:
                    lane.flash(kind)
        h = self.hist.setdefault(result.id, [])
        h.append(status)
        del h[:-HISTORY]
        self.last[result.id] = status
        self.updated.add(result.id)

    def _flash(self, probe_id: str, kind: str) -> None:
        self.flashing[probe_id] = kind

        def done() -> None:
            self.flashing.pop(probe_id, None)
            self.refresh_table()

        self.set_timer(FLASH_SECONDS, done)

    def delta(self, probe_id: str) -> str | None:
        ref = self.baseline.get(probe_id) if self.baseline is not None else self.prev.get(probe_id)
        cur = self.last.get(probe_id)
        if ref is None or cur is None or granted(ref) == granted(cur):
            return None
        return "gained" if granted(cur) else "lost"

    # -- rendering --------------------------------------------------------------

    def _render_score(self, results: list[dict]) -> None:
        allowed = denied = drift = gained = lost = by_sandbox = by_baseline = 0
        per: dict[str, list[int]] = {}
        for r in results:
            v = self.last.get(r["id"])
            c = per.setdefault(r["category"], [0, 0])
            c[1] += 1
            if v is None:
                continue
            if granted(v):
                allowed += 1
                c[0] += 1
            else:
                denied += 1
                kind = (r.get("blocker") or {}).get("kind")
                if kind == "sandbox":
                    by_sandbox += 1
                elif kind:
                    by_baseline += 1
            if r.get("expect") and granted(v) != (r["expect"] == "allowed"):
                drift += 1
            d = self.delta(r["id"])
            gained += d == "gained"
            lost += d == "lost"

        line1 = Text.assemble(
            ("✔ ALLOWED ", f"bold {GREEN}"), (f"{allowed}", f"bold {GREEN}"), "   ",
            ("✘ DENIED ", f"bold {RED}"), (f"{denied}", f"bold {RED}"),
            (" (", "dim"), (f"{by_sandbox} by sandbox", SANDBOX), (f" · {by_baseline} baseline)", "dim"), "   ",
            ("⚠ DRIFT ", f"bold {AMBER}"), (f"{drift}", f"bold {AMBER}"), "   ",
            ("Δ ", "bold"), (f"+{gained}", f"bold {GREEN}"), " ", (f"−{lost}", f"bold {RED}"),
            (" vs baseline" if self.baseline is not None else " since last run", "dim"),
        )
        if self.auto:
            line1.append(f"   ⟳ AUTO {self.state.config.settings.interval_seconds:g}s", style=f"bold {BLUE}")
        self.query_one("#score", Static).update(line1)
        for switch in self.query(CategorySwitch):
            a, n = per.get(switch.category, [0, 0])
            switch.show(switch.category not in self.hidden, a, n)

    def refresh_table(self) -> None:
        snap = self.state.snapshot()
        sysinfo = snap["system"]
        caps = ", ".join(sysinfo["capabilities"][:5])
        if len(sysinfo["capabilities"]) > 5:
            caps += f" +{len(sysinfo['capabilities']) - 5}…"
        landlock = (
            f"ABI v{sysinfo['landlock_abi']}"
            if sysinfo.get("landlock_abi")
            else "unsupported"
        )
        ribbon = Text.assemble(
            (" Host ", "bold reverse"),
            f" {sysinfo['hostname']}  ",
            ("Kernel ", "bold"), f"{sysinfo['kernel']}  ",
            ("User ", "bold"), f"{sysinfo['user']} (uid {sysinfo['uid']})  ",
            ("OpenShell ", "bold"),
            ("YES" if sysinfo["openshell"] else "no",
             "bold yellow" if sysinfo["openshell"] else "dim"),
            "  ",
            ("Runtime ", "bold"), f"{sysinfo['container_runtime'] or 'host'}  ",
            ("Landlock ", "bold"), f"{landlock}  ",
            ("seccomp ", "bold"), f"{sysinfo['seccomp']}  ",
            ("Sandboxed ", "bold"),
            (str(sysinfo["sandboxed"]), "bold yellow" if sysinfo["sandboxed"] else "green"),
            f"\n Capabilities: {caps or 'none'}",
            ("\n uname -a ", "bold"), sysinfo.get("uname", ""),
        )
        self.query_one("#ribbon", Static).update(ribbon)
        self._render_score(snap["results"])

        if self._categories() != self.cats:
            self.call_later(self._rebuild_lanes)
        running = self.state.running
        rows: dict[str, list[dict]] = {}
        for r in snap["results"]:
            rows.setdefault(r["category"], []).append(r)
        for lane in self.query(Lane):
            lane.display = lane.category not in self.hidden
            lane_rows = rows.get(lane.category, [])
            allowed = denied = by_sandbox = 0
            for r in lane_rows:
                v = self.last.get(r["id"])
                if v is None:
                    continue
                if granted(v):
                    allowed += 1
                else:
                    denied += 1
                    by_sandbox += (r.get("blocker") or {}).get("kind") == "sandbox"
            lane.summarize(allowed, denied, by_sandbox, len(lane_rows))
            self._fill_table(lane.table, lane_rows, running)
        self.query_one("#all-hidden").display = bool(self.cats) and all(c in self.hidden for c in self.cats)
        self._render_stage(rows)

    def _render_stage(self, rows: dict[str, list[dict]]) -> None:
        allowed = denied = gained = lost = 0
        for tile in self.query(StageTile):
            tile.display = tile.category not in self.hidden
            tile_rows = []
            for r in rows.get(tile.category, []):
                v = self.last.get(r["id"])
                d = self.delta(r["id"])
                blocker = Blocker(**r["blocker"]) if r.get("blocker") and v and not granted(v) else None
                tile_rows.append((r, v, d, blocker, self.flashing.get(r["id"])))
                if tile.category in self.hidden:
                    continue
                if v is not None:
                    allowed += granted(v)
                    denied += not granted(v)
                gained += d == "gained"
                lost += d == "lost"
            tile.show(tile_rows)
        ran = bool(self.last)
        for key, n in (("allowed", allowed), ("denied", denied), ("granted", gained), ("revoked", lost)):
            self.query_one(f"#st-{key}", Digits).update(str(n) if ran else "–")
        ref = "vs baseline" if self.baseline is not None else "since last run"
        self.query_one("#st-granted-label", Static).update(f"GRANTED {ref}")
        self.query_one("#st-revoked-label", Static).update(f"REVOKED {ref}")
        if len(self.trend) >= 2:
            self.query_one(Sparkline).data = self.trend[-TREND:]

    def _banner(self, kind: str | None = None, message: str = "") -> None:
        """Headline across the top of stage mode: the last access change, else who we are."""
        banner = self.query_one("#st-banner", Static)
        if kind is None:
            sysinfo = self.state.snapshot()["system"]
            landlock = f"Landlock v{sysinfo['landlock_abi']}" if sysinfo.get("landlock_abi") else "no Landlock"
            enforced = "OpenShell sandbox" if sysinfo["openshell"] else "sandboxed" if sysinfo["sandboxed"] else "unsandboxed"
            banner.update(Text.assemble(
                ("CAN THE AGENT…?   ", f"bold {GREEN}"),
                (f"{sysinfo['hostname']}", "bold"),
                (f"  ·  {enforced}  ·  {landlock}  ·  seccomp {sysinfo['seccomp']}  ·  uid {sysinfo['uid']}", "dim"),
            ))
            banner.styles.background = None
            return
        color = GREEN if kind == "gained" else RED
        banner.update(Text(message, style="bold #04140a" if kind == "gained" else "bold white"))
        banner.styles.background = color
        if self._banner_timer is not None:
            self._banner_timer.stop()
        self._banner_timer = self.set_timer(BANNER_SECONDS, lambda: self._banner())

    def _fill_table(self, table: DataTable, rows: list[dict], running: bool) -> None:
        cursor_key = None
        if table.row_count:
            try:
                cursor_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:  # cursor outside the table
                cursor_key = None
        table.clear()
        for r in rows:
            pid = r["id"]
            last = self.last.get(pid)
            if last is None:
                status = status_cell(r["status"])
            elif running and pid not in self.updated:
                # mid-run: keep the previous verdict on screen, marked as being re-probed
                label, _ = STATUS_STYLES[Status(last)]
                status = Text(f"⟳ {label[2:]}", style=f"dim {BLUE}")
            else:
                status = status_cell(last)
            name = Text(r["name"])
            flash = self.flashing.get(pid)
            if flash == "lost":
                name.stylize(f"bold white on {RED}")
            elif flash == "gained":
                name.stylize(f"bold #04140a on {GREEN}")
            blocker = Blocker(**r["blocker"]) if r.get("blocker") and last and not granted(last) else None
            latency = f"{r['latency_ms']:.0f} ms" if r["latency_ms"] else "—"
            table.add_row(
                status,
                delta_cell(self.delta(pid)),
                name,
                blocker_cell(blocker),
                str(r["exit_code"]) if r["exit_code"] is not None else "—",
                latency,
                history_cell(self.hist.get(pid, [])),
                key=pid,
            )
        if table.row_count:
            row = 0
            if cursor_key is not None:
                try:
                    row = table.get_row_index(cursor_key)
                except Exception:  # row filtered out
                    row = 0
            table.move_cursor(row=row)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        if event.data_table.has_focus:
            self.show_detail(str(event.row_key.value))

    def on_descendant_focus(self, event) -> None:
        # tabbing into another category box: inspect the row its cursor is on
        table = event.widget
        if isinstance(table, DataTable) and table.row_count:
            try:
                key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
            except Exception:  # cursor outside the table
                return
            self.show_detail(str(key))

    def show_detail(self, probe_id: str) -> None:
        result = self.state.results.get(probe_id)
        if result is None:
            return
        detail = Text.assemble(
            (f"{result.name} ", "bold"),
            (f"[{result.status.value.upper()}] ", "bold"),
            category_cell(result.category), "  ",
            f"exit code: {result.exit_code if result.exit_code is not None else '—'}  ",
            f"latency: {result.latency_ms:.0f} ms\n",
            ("$ ", "dim"), (result.command, "dim"), "\n",
        )
        d = self.delta(result.id)
        if d:
            ref = self.baseline.get(result.id) if self.baseline is not None else self.prev.get(result.id)
            where = "at baseline" if self.baseline is not None else "last run"
            detail.append_text(delta_cell(d))
            detail.append(f" was {str(ref).upper()} {where}\n")
        if result.blocker:
            glyph, style = BLOCKER_STYLES[result.blocker.kind]
            who = {"sandbox": "enforced by the sandbox", "baseline": "denied without a sandbox too",
                   "unknown": "cause unclear"}[result.blocker.kind]
            detail.append("blocked by: ")
            detail.append(f"{glyph} {result.blocker.layer}", style=style)
            detail.append(f" ({who}) — {result.blocker.reason}\n", style="dim")
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

    def action_toggle_auto(self) -> None:
        self.auto = not self.auto
        self.notify("auto-run on" if self.auto else "auto-run off", timeout=2)
        if self.auto:
            self.action_run_probes()
        self.refresh_table()

    def action_toggle_baseline(self) -> None:
        if self.baseline is None:
            self.baseline = dict(self.last)
            self.notify("baseline pinned — Δ now compares against it", timeout=3)
        else:
            self.baseline = None
            self.notify("baseline cleared — Δ compares against the last run", timeout=3)
        self.refresh_table()

    def action_toggle_cat(self, category: str) -> None:
        if category not in self.cats:
            return
        self.hidden.symmetric_difference_update({category})
        _, label, _ = cat_meta(category)
        self.notify(f"{label} {'hidden' if category in self.hidden else 'shown'}", timeout=1.5)
        self.refresh_table()
        focused = self.focused
        if focused is None or not focused.display or not focused.parent.display:
            visible = [lane for lane in self.query(Lane) if lane.display]
            if visible:
                visible[0].table.focus()

    def action_toggle_cat_key(self, key: int) -> None:
        for cat in self.cats:
            if self.cat_key(cat) == key:
                self.action_toggle_cat(cat)

    def on_resize(self) -> None:
        self.call_after_refresh(self.refresh_table)  # breakpoint classes may have changed

    def action_toggle_stage(self) -> None:
        self.stage = not self.stage
        self.set_class(self.stage, "stage")
        self.refresh_table()

    def action_show_all(self) -> None:
        self.hidden.clear()
        self.refresh_table()

    def _summarize_run(self) -> None:
        lost = [r for kind, r in self.run_flips if kind == "lost"]
        granted_now = [r for kind, r in self.run_flips if kind == "gained"]
        gained = len(granted_now)
        if lost:
            layers = sorted({r.blocker.layer for r in lost if r.blocker and r.blocker.kind == "sandbox"})
            self._banner("lost", f"▼ ACCESS REVOKED   {', '.join(r.name for r in lost)}"
                                 + (f"   — enforced by {', '.join(layers)}" if layers else ""))
        elif granted_now:
            self._banner("gained", f"▲ ACCESS GRANTED   {', '.join(r.name for r in granted_now)}")
        if lost:
            layers = sorted({r.blocker.layer for r in lost if r.blocker})
            by = f" by {', '.join(layers)}" if layers else ""
            noun = "capability" if len(lost) == 1 else "capabilities"
            self.notify(f"🔒 {len(lost)} {noun} LOCKED DOWN{by}", severity="error", timeout=6)
            self.bell()
        if gained:
            noun = "capability" if gained == 1 else "capabilities"
            self.notify(f"🔓 {gained} {noun} OPENED UP", severity="information", timeout=6)

    def _on_state_event(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "run_start":
            self.run_flips = []
            self.updated = set()
        elif kind == "probe":
            result = self.state.results.get(event["data"]["id"])
            if result is not None:
                self._ingest(result, live=True)
        elif kind == "run_complete":
            self.trend.append(sum(granted(v) for v in self.last.values()))
            del self.trend[:-TREND]
            self._summarize_run()
            if self.auto:
                self.set_timer(self.state.config.settings.interval_seconds, self.action_run_probes)
        self.refresh_table()

    async def _event_pump(self) -> None:
        queue = self.state.subscribe()
        try:
            while True:
                self._on_state_event(await queue.get())
        finally:
            self.state.unsubscribe(queue)
