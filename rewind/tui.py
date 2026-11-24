"""rewind's interactive cockpit (Textual).

Boot screen rebuilds the session with live progress; the main screen has a
timeline, a step view (diff / file at this step / output), a side panel with
an animated context gauge and a file heat-map, and a scrubber across the
bottom. Space replays the session; b bisects it live.
"""

from __future__ import annotations

import difflib
import math
import os
import tempfile
import time
from pathlib import Path

from rich.color import Color
from rich.console import Group
from rich.panel import Panel
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.containers import ScrollableContainer
from textual.widgets import (
    Button, DataTable, Footer, Input, Label, ProgressBar, RichLog, Static, TabbedContent, TabPane,
)

from . import render as R
from .model import Session
from .reconstruct import Mutation, Timeline, build_timeline, known
from .sandbox import Sandbox, SandboxError, choose_root, export_files
from .transcript import load_session

# Tokyo-night-ish palette, used for Rich renderables.
C = {
    "fg": "#c0caf5", "dim": "#565f89", "faint": "#3b4261", "cyan": "#7dcfff", "blue": "#7aa2f7",
    "magenta": "#bb9af7", "green": "#9ece6a", "yellow": "#e0af68", "orange": "#ff9e64", "red": "#f7768e",
    "teal": "#73daca", "add_bg": "#1c2b24", "del_bg": "#33202a", "hunk": "#3d59a1",
}
KIND = {
    "prompt": ("❯", C["cyan"]), "edit": ("✎", C["yellow"]), "write": ("✚", C["green"]),
    "shell": ("$", C["magenta"]), "read": ("◌", C["dim"]), "tool": ("·", C["dim"]), "compact": ("⇣", C["red"]),
}
KIND_PRIORITY = ["compact", "write", "edit", "prompt", "shell", "read", "tool"]
LOGO = [
    "█▀█ █▀▀ █ █ █ █ █▄ █ █▀▄",
    "█▀▄ ██▄ ▀▄▀▄▀ █ █ ▀█ █▄▀",
]
SPEEDS = [1, 2, 4, 8, 16, 32]


def blend(a: str, b: str, t: float) -> str:
    ca, cb = Color.parse(a).triplet, Color.parse(b).triplet
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(x + (y - x) * t) for x, y in zip(ca, cb))


def heat(t: float) -> str:
    """green → yellow → red."""
    return blend(C["green"], C["yellow"], t * 2) if t < 0.5 else blend(C["yellow"], C["red"], (t - 0.5) * 2)


def lexer_for(path: str) -> str:
    try:
        return Syntax.guess_lexer(path)
    except Exception:  # noqa: BLE001 - any lexer lookup failure falls back to text
        return "text"


# ------------------------------------------------------------------ widgets


class Logo(Widget):
    """The rewind wordmark with a moving gradient."""

    phase = reactive(0.0)

    def on_mount(self) -> None:
        self.set_interval(1 / 24, self._tick)

    def _tick(self) -> None:
        self.phase += 0.18

    def render(self) -> Text:
        out = Text(justify="center")
        stops = [C["blue"], C["magenta"], C["cyan"], C["teal"]]
        for row, line in enumerate(LOGO):
            for x, ch in enumerate(line):
                t = (math.sin(x * 0.22 - self.phase + row * 0.6) + 1) / 2 * (len(stops) - 1)
                i = min(int(t), len(stops) - 2)
                out.append(ch, style=Style(color=blend(stops[i], stops[i + 1], t - i), bold=True))
            out.append("\n")
        return out


class Gauge(Widget):
    """Animated context-window meter."""

    value = reactive(0.0)
    peak = reactive(1.0)
    cache = reactive(0.0)
    delta = reactive(0)

    def render(self) -> Text:
        w = max(10, self.size.width - 2)
        frac = self.value / self.peak if self.peak else 0
        fill = frac * w
        t = Text()
        for i in range(w):
            if i < int(fill):
                t.append("█", style=heat(i / w))
            elif i < fill:
                t.append("▌", style=heat(i / w))
            else:
                t.append("░", style=C["faint"])
        t.append("\n")
        t.append(f"{R.tokens(int(self.value)):>6}", style=Style(color=C["fg"], bold=True))
        t.append(f" of {R.tokens(int(self.peak))} peak", style=C["dim"])
        if self.delta:
            sign = "+" if self.delta > 0 else "−"
            t.append(f"  {sign}{R.tokens(abs(self.delta))}", style=C["red"] if self.delta > 0 else C["green"])
        t.append("\n")
        t.append("cache ", style=C["dim"])
        cw = max(4, w - 12)
        cf = int(self.cache * cw)
        t.append("━" * cf, style=C["teal"])
        t.append("━" * (cw - cf), style=C["faint"])
        t.append(f" {self.cache:.0%}", style=C["teal"])
        return t


class Scrubber(Widget):
    """Every step of the session across the full width, with context and bisect lanes."""

    cursor = reactive(0)

    def __init__(self, session: Session, timeline: Timeline, **kw):
        super().__init__(**kw)
        self.session = session
        self.timeline = timeline
        n = len(session.steps)
        ctx, last = [], 0
        for s in session.steps:
            last = s.context or last
            ctx.append(last)
        self.ctx = ctx
        self.peak = max(ctx) if ctx else 1
        self.n = max(n, 1)
        self.verdicts: dict[int, str] = {}  # step index -> good/bad/skip
        self.window: tuple[int, int] | None = None  # step range under search
        self.culprit: int | None = None
        self.probe: int | None = None

    def _bins(self, width: int) -> list[range]:
        # Always span the full width: short sessions give each step several columns.
        width = max(1, width)
        return [range(min(int(i * self.n / width), self.n - 1), max(min(int((i + 1) * self.n / width), self.n),
                      min(int(i * self.n / width), self.n - 1) + 1)) for i in range(width)]

    def render(self) -> Text:
        width = self.size.width
        bins = self._bins(width)
        steps = self.session.steps
        cur_bin = min(range(len(bins)), key=lambda i: 0 if self.cursor in bins[i] else abs(bins[i].start - self.cursor))
        lanes = [Text(), Text(), Text(), Text(), Text()]
        for bi, rng in enumerate(bins):
            kinds = {steps[i].kind for i in rng if i < len(steps)}
            kind = next((k for k in KIND_PRIORITY if k in kinds), "tool")
            glyph = {"prompt": "┃", "edit": "▆", "write": "█", "shell": "▂", "compact": "▼"}.get(kind, "▁")
            col = KIND[kind][1]
            here = bi == cur_bin
            lanes[0].append(glyph, style=Style(color=col, bgcolor="#292e42" if here else None, bold=here))
            # context area chart, two rows = 16 levels
            v = max(self.ctx[i] for i in rng) if rng.stop <= len(self.ctx) else 0
            level = int(v / self.peak * 16) if self.peak else 0
            colour = heat(v / self.peak if self.peak else 0)
            top = R.SPARKS[level - 9] if level > 8 else " "
            bottom = "█" if level >= 8 else (R.SPARKS[max(level - 1, 0)] if level else " ")
            bg = "#292e42" if here else None
            lanes[1].append(top, style=Style(color=colour, bgcolor=bg))
            lanes[2].append(bottom, style=Style(color=colour, bgcolor=bg))
            # bisect lane
            vs = [self.verdicts[i] for i in rng if i in self.verdicts]
            if self.culprit is not None and self.culprit in rng:
                lanes[3].append("✗", style=Style(color="#ffffff", bgcolor=C["red"], bold=True))
            elif self.probe is not None and self.probe in rng:
                lanes[3].append("◆", style=Style(color=C["cyan"], bold=True))
            elif "bad" in vs:
                lanes[3].append("█", style=C["red"])
            elif "good" in vs:
                lanes[3].append("█", style=C["green"])
            elif "skip" in vs:
                lanes[3].append("█", style=C["yellow"])
            elif self.window and any(self.window[0] < i <= self.window[1] for i in rng):
                lanes[3].append("▒", style=C["fg"])
            else:
                lanes[3].append("─" if self.window else " ", style=C["faint"])
        # cursor lane with a label that follows the playhead
        s = steps[self.cursor] if steps else None
        label = f"▲ #{self.cursor + 1}  {R.when(s.timestamp) if s else ''}  turn {s.turn if s else 0}"
        start = max(0, min(cur_bin - 1, width - len(label)))
        lanes[4].append(" " * start)
        lanes[4].append(label, style=Style(color=C["cyan"], bold=True))
        out = Text()
        for i, lane in enumerate(lanes):
            out.append_text(lane)
            if i < len(lanes) - 1:
                out.append("\n")
        return out

    def on_click(self, event: events.Click) -> None:
        off = event.get_content_offset(self)
        if off is None:
            return
        bins = self._bins(self.size.width)
        if 0 <= off.x < len(bins):
            self.screen.jump(bins[off.x].start)  # type: ignore[attr-defined]


# ------------------------------------------------------------------ screens


class BootScreen(Screen):
    """Rebuilds the session on a worker thread while showing progress."""

    CSS = """
    BootScreen { align: center middle; background: $background; }
    #boot { width: 72; height: auto; padding: 1 2; border: round $primary; background: $surface; }
    #logo { height: 3; width: 100%; content-align: center middle; text-align: center; }
    #tag { width: 100%; content-align: center middle; color: $text-muted; }
    #credit { width: 100%; content-align: center middle; color: $secondary; text-style: italic; margin-bottom: 1; }
    #stage { width: 100%; height: 2; color: $text; }
    #bar { width: 100%; margin-top: 1; }
    #facts { width: 100%; height: auto; margin-top: 1; color: $text-muted; }
    """

    def __init__(self, loader):
        super().__init__()
        self.loader = loader

    def compose(self) -> ComposeResult:
        with Vertical(id="boot"):
            yield Logo(id="logo")
            yield Label("flight recorder for AI coding agent sessions", id="tag")
            yield Label("engineered by Krish Desai", id="credit")
            yield Static("reading the transcript…", id="stage")
            yield ProgressBar(total=None, show_eta=False, id="bar")
            yield Static("", id="facts")

    def on_mount(self) -> None:
        self.started = time.perf_counter()
        self.rw_app = self.app  # worker threads cannot read self.app
        self.load()

    @work(thread=True, exclusive=True)
    def load(self) -> None:
        def stage(text: str) -> None:
            self.rw_app.call_from_thread(lambda: self.query_one("#stage", Static).update(Text(text, style=C["fg"])))

        def facts(text: Text) -> None:
            self.rw_app.call_from_thread(lambda: self.query_one("#facts", Static).update(text))

        def progress(done: int, total: int, path: str) -> None:
            def ui():
                self.query_one("#bar", ProgressBar).update(total=total, progress=done)
                self.query_one("#stage", Static).update(Text.assemble(
                    ("rebuilding every version of ", C["dim"]), (os.path.basename(path), C["cyan"]),
                    (f"  {done}/{total}", C["dim"])))
            self.rw_app.call_from_thread(ui)

        session, timeline = self.loader(stage, facts, progress)
        remaining = 1.4 - (time.perf_counter() - self.started)
        if remaining > 0:
            time.sleep(remaining)
        self.rw_app.call_from_thread(lambda: self.rw_app.switch_screen(MainScreen(session, timeline)))


class BisectPrompt(ModalScreen[str | None]):
    CSS = """
    BisectPrompt { align: center middle; background: $background 70%; }
    #box { width: 90; max-width: 95%; height: auto; padding: 0 2 1 2; border: thick $error; background: $surface; }
    #head { height: 3; }
    #title { width: 1fr; color: $error; text-style: bold; content-align: left middle; height: 3; }
    #close { min-width: 5; width: 5; height: 3; border: none; background: $surface; color: $text-muted; }
    #close:hover { background: $error; color: $text; }
    #help { color: $text-muted; margin-bottom: 1; }
    #cmd { margin-bottom: 1; }
    #buttons { height: 3; align: right middle; }
    #buttons Button { margin-left: 2; min-width: 16; }
    """
    BINDINGS = [Binding("escape", "cancel", "cancel")]

    def __init__(self, default: str):
        super().__init__()
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="box"):
            with Horizontal(id="head"):
                yield Label("◆ BISECT THIS SESSION", id="title")
                yield Button("✕", id="close", tooltip="close (Esc)")
            yield Label("A command that passes on good code and fails on bad code (exit 125 = can't test). "
                        "It runs in a throwaway copy of the project; your files are not touched.", id="help")
            yield Input(value=self.default, placeholder="e.g. python -m pytest -q tests/test_pricing.py", id="cmd")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel", variant="default")
                yield Button("▶  Run bisect", id="run", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cmd", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "run":
            self.dismiss(self.query_one("#cmd", Input).value.strip() or None)
        else:
            self.dismiss(None)

    def on_click(self, event: events.Click) -> None:
        # a click on the dimmed background (outside the box) closes the popup
        if event.widget is self:
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MainScreen(Screen):
    CSS = """
    MainScreen { layout: vertical; background: $background; }
    #topbar { height: 1; background: $panel; padding: 0 1; }
    #body { height: 1fr; }
    #timeline { width: 34%; min-width: 40; border: round $primary-darken-2; border-title-color: $primary;
                background: $background; scrollbar-size-vertical: 1; }
    #timeline:focus { border: round $primary; }
    #center { width: 1fr; }
    #card { height: auto; max-height: 11; border: round $secondary-darken-2; border-title-color: $secondary;
            padding: 0 1; background: $background; }
    #tabs { height: 1fr; }
    .pane { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #side { width: 38; }
    #gauge { height: 5; border: round $primary-darken-2; border-title-color: $primary; padding: 0 1; }
    #turn { height: auto; max-height: 8; border: round $primary-darken-2; border-title-color: $primary; padding: 0 1; }
    #heat { height: 1fr; border: round $primary-darken-2; border-title-color: $primary; padding: 0 1; }
    #blog { height: 14; border: round $error; border-title-color: $error; display: none; padding: 0 1;
            scrollbar-size-vertical: 1; }
    #blog.active { display: block; }
    MainScreen.narrow #side { display: none; }
    MainScreen.narrower #timeline { width: 30; min-width: 30; }
    MainScreen.zoom #timeline, MainScreen.zoom #side, MainScreen.zoom #card { display: none; }
    #filepane, #diffpane, #outpane { overflow-x: auto; overflow-y: auto; scrollbar-size-horizontal: 1; }
    #file, #diff { width: auto; min-width: 100%; }
    #scrubber { height: 7; border: round $primary-darken-2; border-title-color: $primary; padding: 0 1; }
    """
    BINDINGS = [
        Binding("space", "play", "play/pause"),
        Binding("plus,equals_sign", "faster", "faster", show=False),
        Binding("minus", "slower", "slower", show=False),
        Binding("b", "bisect", "bisect"),
        Binding("n", "next_prompt", "next prompt"),
        Binding("p", "prev_prompt", "prev prompt", show=False),
        Binding("right_square_bracket", "next_edit", "next edit", show=False),
        Binding("left_square_bracket", "prev_edit", "prev edit", show=False),
        Binding("e", "edits_only", "edits only"),
        Binding("a", "show_all", "all steps", show=False),
        Binding("1", "tab('tab-diff')", "diff", show=False),
        Binding("2", "tab('tab-file')", "file", show=False),
        Binding("3", "tab('tab-out')", "output", show=False),
        Binding("f", "tab('tab-file')", "file@step"),
        Binding("z", "zoom", "zoom"),
        Binding("shift+right,l", "hscroll(1)", "⇧→ scroll", show=False),
        Binding("shift+left,h", "hscroll(-1)", "⇧← scroll", show=False),
        Binding("s", "side", "side panel", show=False),
        Binding("x", "export", "export"),
        Binding("g", "first", "first", show=False),
        Binding("G", "last", "last", show=False),
        Binding("j", "down", show=False),
        Binding("k", "up", show=False),
        Binding("q", "app.quit", "quit"),
    ]

    def __init__(self, session: Session, timeline: Timeline):
        super().__init__()
        self.session = session
        self.timeline = timeline
        self.steps = session.steps
        self.mut_by_step = {m.step.index: m for m in timeline.mutations}
        self.state_after = []
        j = 0
        for s in self.steps:
            if s.index in self.mut_by_step:
                j += 1
            self.state_after.append(j)
        self.mode = "default"
        self.rows: list[int] = []
        self.row_of: dict[int, int] = {}
        self.current = 0
        self.speed_i = 2
        self.timer = None
        self.bisecting = False
        self.culprit: Mutation | None = None
        self.culprit_output = ""
        self.culprit_runs = 0
        self.root = None
        self.peak = max((c.usage.context for c in session.calls if c.agent == "main"), default=1)
        self.turns = max((s.turn for s in self.steps), default=0)
        ctx, last = [], 0
        for s in self.steps:
            last = s.context or last
            ctx.append(last)
        self.ctx = ctx

    def _root(self) -> str | None:
        try:
            return choose_root(self.timeline, self.app.root_override)  # type: ignore[attr-defined]
        except SandboxError:
            return self.session.cwd or None

    # ---------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Static(id="topbar")
        with Horizontal(id="body"):
            yield DataTable(id="timeline", cursor_type="row", show_header=False, zebra_stripes=False)
            with Vertical(id="center"):
                yield Static(id="card")
                with TabbedContent(id="tabs", initial="tab-diff"):
                    with TabPane("diff", id="tab-diff"):
                        with ScrollableContainer(classes="pane", id="diffpane"):
                            yield Static(id="diff")
                    with TabPane("file at this step", id="tab-file"):
                        with ScrollableContainer(classes="pane", id="filepane"):
                            yield Static(id="file")
                    with TabPane("output", id="tab-out"):
                        with ScrollableContainer(classes="pane", id="outpane"):
                            yield Static(id="out")
            with Vertical(id="side"):
                yield Gauge(id="gauge")
                yield Static(id="turn")
                yield Static(id="heat")
                yield RichLog(id="blog", wrap=True, markup=False, highlight=False)
        yield Scrubber(self.session, self.timeline, id="scrubber")
        yield Footer()

    def on_mount(self) -> None:
        self.rw_app = self.app  # worker threads cannot read self.app
        self.root = self._root()
        self._layout(self.app.size.width)
        good, total = self.timeline.fidelity()
        t = self.query_one("#timeline", DataTable)
        t.border_title = "timeline"
        t.add_column("n", width=5)
        t.add_column("k", width=1)
        t.add_column("what", width=44)
        t.add_column("d", width=8)
        self.query_one("#card").border_title = "step"
        self.query_one("#gauge").border_title = "context window"
        self.query_one("#turn").border_title = "turn"
        self.query_one("#heat").border_title = "files · heat"
        self.query_one("#blog").border_title = "bisect"
        sc = self.query_one("#scrubber")
        sc.border_title = f"session · {len(self.steps)} steps · {len(self.timeline.mutations)} edits · " \
                          f"rebuilt {good}/{total} exact · click to jump"
        self.query_one("#gauge", Gauge).peak = self.peak
        self._fill()
        t.focus()
        self._show(0)
        if self.app.check:  # type: ignore[attr-defined]
            self.notify("Press b to bisect with the check you gave, or space to replay the session.",
                        title="ready", timeout=6)

    def _fill(self) -> None:
        t = self.query_one("#timeline", DataTable)
        t.clear()
        self.rows, self.row_of = [], {}
        for s in self.steps:
            if self.mode == "default" and s.kind in ("read", "tool") and not s.error:
                continue
            if self.mode == "edits" and s.kind not in ("prompt", "edit", "write"):
                continue
            icon, col = KIND.get(s.kind, ("·", C["dim"]))
            if s.kind == "prompt":
                what = Text.assemble((f"T{s.turn} ", Style(color="#1a1b26", bgcolor=C["cyan"], bold=True)), " ",
                                     (R.short(s.text, 40), Style(color=C["cyan"], bold=True)))
            elif s.kind in ("edit", "write", "read"):
                name = R.rel(s.path or "", self.root)
                what = Text.assemble((os.path.basename(name), Style(color=C["fg"])),
                                     (" " + R.short(os.path.dirname(name), 28), C["dim"]))
            elif s.kind == "shell":
                what = Text(R.short(s.text, 44), style=C["magenta"])
            elif s.kind == "compact":
                what = Text(R.short(s.text, 44), style=C["red"])
            else:
                what = Text(f"{s.tool} {R.short(s.text, 36)}", style=C["dim"])
            if s.error:
                what.append(" ✗", style=C["red"])
            if s.agent != "main":
                what.append(" ⑂", style=C["blue"])
            delta = Text()
            m = self.mut_by_step.get(s.index)
            if m is not None:
                a, r = m.changed_lines
                ga = min(5, math.ceil(math.log2(a + 1))) if a else 0
                gr = min(3, math.ceil(math.log2(r + 1))) if r else 0
                delta.append("▮" * ga, style=C["green"])
                delta.append("▮" * gr, style=C["red"])
            t.add_row(Text(str(s.number), style=C["dim"]), Text(icon, style=col), what, delta, key=str(s.index))
            self.row_of[s.index] = len(self.rows)
            self.rows.append(s.index)

    # ---------------------------------------------------------- navigation

    def jump(self, index: int) -> None:
        if not self.is_attached or self.app.screen is not self:
            return
        index = max(0, min(index, len(self.steps) - 1))
        if index not in self.row_of:
            # nearest visible row at or before it, else switch to all steps
            before = [i for i in self.rows if i <= index]
            if before and index - before[-1] < 3:
                index = before[-1]
            else:
                self.mode = "all"
                self._fill()
        t = self.query_one("#timeline", DataTable)
        t.move_cursor(row=self.row_of[index], animate=False, scroll=True)
        self._show(index)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        idx = int(event.row_key.value)
        if idx != self.current:
            self._show(idx)

    def _show(self, index: int) -> None:
        self.current = index
        s = self.steps[index]
        m = self.mut_by_step.get(index)
        self._topbar()
        self._card(s, m)
        self._diff(s, m)
        tabs = self.query_one("#tabs", TabbedContent)
        if tabs.active == "tab-file":
            self._file(s, m)
        self._output(s, m)
        self._gauge(index)
        self._turn(s)
        self._heat(index)
        sc = self.query_one("#scrubber", Scrubber)
        sc.cursor = index

    # ---------------------------------------------------------- panels

    def _topbar(self) -> None:
        s = self.steps[self.current]
        left = Text()
        left.append(" ◆ rewind ", style=Style(color="#1a1b26", bgcolor=C["magenta"], bold=True))
        left.append(" by Krish Desai", style=Style(color=C["magenta"], italic=True))
        left.append(f"  {R.short(self.session.title, 48)}", style=Style(color=C["fg"], bold=True))
        if self.session.branch:
            left.append(f"   ⎇ {self.session.branch}", style=C["dim"])
        right = Text()
        if self.bisecting:
            right.append(" ● BISECTING ", style=Style(color="#1a1b26", bgcolor=C["red"], bold=True))
            right.append("  ")
        elif self.culprit is not None:
            right.append(f" ✗ culprit #{self.culprit.step.number} ", style=Style(color="#ffffff", bgcolor=C["red"], bold=True))
            right.append("  ")
        if self.timer is not None:
            right.append(f" ▶ {SPEEDS[self.speed_i]}× ", style=Style(color="#1a1b26", bgcolor=C["green"], bold=True))
            right.append("  ")
        right.append(f"step {s.number}/{len(self.steps)}", style=C["fg"])
        right.append(f"  ·  turn {s.turn}/{self.turns}", style=C["dim"])
        right.append(f"  ·  {R.when(s.timestamp, True)}", style=C["dim"])
        grid = Table.grid(expand=True)
        grid.add_column()
        grid.add_column(justify="right")
        grid.add_row(left, right)
        self.query_one("#topbar", Static).update(grid)

    def _card(self, s, m: Mutation | None) -> None:
        card = self.query_one("#card", Static)
        parts = []
        if self.culprit is not None and m is self.culprit:
            banner = Text.assemble(
                (" ✗ FIRST BAD EDIT ", Style(color="#ffffff", bgcolor=C["red"], bold=True)),
                (f"  found by bisect in {self.culprit_runs} runs · the check passes right before this edit "
                 f"and fails right after it", C["red"]))
            parts.append(banner)
        icon, col = KIND.get(s.kind, ("·", C["dim"]))
        head = Text()
        head.append(f"{icon} ", style=Style(color=col, bold=True))
        if s.kind == "prompt":
            head.append(f"you, turn {s.turn}", style=Style(color=C["cyan"], bold=True))
        else:
            head.append(f"{s.tool or s.kind} ", style=Style(color=col, bold=True))
            head.append(R.rel(s.path or s.text, self.root) if s.kind != "shell" else "", style=C["fg"])
        if m is not None:
            a, r = m.changed_lines
            head.append(f"   +{a}", style=C["green"])
            head.append(f" −{r}", style=C["red"])
            q = m.after_q
            head.append(f"   {q}", style=C["green"] if q in ("exact", "derived") else C["yellow"])
        if s.agent != "main":
            head.append(f"   ⑂ subagent {s.agent[:7]}", style=C["blue"])
        parts.append(head)
        if s.kind == "prompt":
            parts.append(Text(R.short(s.text, 900), style=C["fg"]))
        else:
            p = self.session.prompt_for_turn(s.turn)
            if p:
                parts.append(Text.assemble(("you  ", Style(color=C["cyan"], bold=True)), (R.short(p.text, 260), C["cyan"])))
            if s.why:
                parts.append(Text.assemble(("agent  ", Style(color=C["magenta"], bold=True)), (R.short(s.why, 320), C["fg"])))
            if s.kind == "shell":
                parts.append(Text("$ " + R.short(s.text, 300), style=C["magenta"]))
        card.border_title = f"step #{s.number}"
        card.update(Group(*parts))

    def _diff(self, s, m: Mutation | None) -> None:
        w = self.query_one("#diff", Static)
        if m is None:
            if s.kind == "prompt":
                n_steps = sum(1 for x in self.steps if x.turn == s.turn) - 1
                n_edits = sum(1 for x in self.timeline.mutations if x.step.turn == s.turn)
                w.update(Text(f"\nThis turn: {n_steps} steps, {n_edits} edits.\nPress ] to jump to its first edit.",
                              style=C["dim"]))
            else:
                w.update(Text("\n(this step changed no files)", style=C["dim"]))
            return
        pane = self.query_one("#diffpane", ScrollableContainer)
        w.update(diff_table(m, lexer_for(m.path), max(pane.size.width - 3, 40)))
        pane.scroll_home(animate=False)

    def _file(self, s, m: Mutation | None) -> None:
        w = self.query_one("#file", Static)
        target = m
        if target is None:
            prev = [x for x in self.timeline.mutations if x.step.index <= s.index]
            target = prev[-1] if prev else None
        if target is None:
            w.update(Text("\n(no file edited yet at this step)", style=C["dim"]))
            return
        j = self.state_after[s.index]
        content, q = self.timeline.file_state(target.key, j)
        if not isinstance(content, str):
            w.update(Text(f"\n{R.rel(target.path, self.root)} does not exist at this step ({q}).", style=C["dim"]))
            return
        changed = set()
        if target.step.index == s.index and isinstance(target.before, str):
            sm = difflib.SequenceMatcher(None, target.before.splitlines(), content.splitlines(), autojunk=False)
            for op, _i1, _i2, j1, j2 in sm.get_opcodes():
                if op != "equal":
                    changed.update(range(j1 + 1, j2 + 1))
        syn = Syntax(content, lexer_for(target.path), theme="monokai", line_numbers=True, highlight_lines=changed,
                     background_color="default", word_wrap=False, indent_guides=True)
        title = Text.assemble((R.rel(target.path, self.root), Style(color=C["fg"], bold=True)),
                              (f"  as of step #{s.number} · {len(content.splitlines())} lines · {q}", C["dim"]))
        w.update(Group(title, Text(""), syn))
        pane = self.query_one("#filepane", ScrollableContainer)
        first = min(changed) if changed else 0
        self.call_after_refresh(pane.scroll_to, y=max(0, first - 6), animate=True, duration=0.35)

    def _output(self, s, m: Mutation | None) -> None:
        w = self.query_one("#out", Static)
        parts = []
        if self.culprit is not None and m is self.culprit and self.culprit_output:
            parts.append(Text("check output right after this edit", style=Style(color=C["red"], bold=True)))
            parts.append(Text(self.culprit_output, style=C["red"]))
            parts.append(Text(""))
        if s.kind == "shell":
            parts.append(Text("$ " + s.text, style=Style(color=C["magenta"], bold=True)))
        if s.output:
            parts.append(Text(f"{s.output_chars:,} chars ≈ {s.output_chars // 4:,} tokens", style=C["dim"]))
            parts.append(Text(s.output[-6000:], style=C["fg"]))
        w.update(Group(*parts) if parts else Text("(no output)", style=C["dim"]))

    def _gauge(self, index: int) -> None:
        g = self.query_one("#gauge", Gauge)
        value = self.ctx[index]
        prev = self.ctx[index - 1] if index else 0
        call = next((c for c in reversed(self.session.calls) if c.step_index <= index and c.agent == "main"), None)
        g.cache = call.usage.cache_ratio if call else 0.0
        g.delta = value - prev
        g.animate("value", float(value), duration=0.35, easing="out_cubic")

    def _turn(self, s) -> None:
        turn_steps = [x for x in self.steps if x.turn == s.turn]
        edits = [x for x in self.timeline.mutations if x.step.turn == s.turn]
        files = {x.key for x in edits}
        t = Text()
        t.append(f"{s.turn}", style=Style(color=C["cyan"], bold=True))
        t.append(f" / {self.turns}", style=C["dim"])
        if turn_steps:
            t.append(f"   {R.when(turn_steps[0].timestamp)} → {R.when(turn_steps[-1].timestamp)}\n", style=C["dim"])
        t.append(f"{len(turn_steps)} steps · {len(edits)} edits · {len(files)} files\n", style=C["fg"])
        done = sum(1 for x in turn_steps if x.index <= s.index)
        w = 34
        f = int(done / max(len(turn_steps), 1) * w)
        t.append("━" * f, style=C["cyan"])
        t.append("━" * (w - f), style=C["faint"])
        self.query_one("#turn", Static).update(t)

    def _heat(self, index: int) -> None:
        counts: dict[str, int] = {}
        last: dict[str, int] = {}
        names: dict[str, str] = {}
        cur_seq = -1
        cur_key = None
        for m in self.timeline.mutations:
            if m.step.index > index:
                break
            counts[m.key] = counts.get(m.key, 0) + 1
            last[m.key] = m.seq
            names[m.key] = m.path
            cur_seq = m.seq
            if m.step.index == index:
                cur_key = m.key
        if not counts:
            self.query_one("#heat", Static).update(Text("no files edited yet", style=C["dim"]))
            return
        top = sorted(counts, key=lambda k: (-(cur_seq - last[k] < 6), -counts[k], -last[k]))[:16]
        mx = max(counts.values())
        out = Text()
        for k in top:
            age = cur_seq - last[k]
            glow = max(0.0, 1 - age / 10)
            colour = blend(C["dim"], C["orange"], glow) if k != cur_key else C["yellow"]
            mark = "▶" if k == cur_key else ("•" if glow > 0 else " ")
            out.append(f"{mark} ", style=Style(color=colour, bold=k == cur_key))
            out.append(f"{R.short(os.path.basename(names[k]), 20):<20} ", style=Style(color=colour, bold=k == cur_key))
            bar = max(1, int(counts[k] / mx * 9))
            out.append("▰" * bar, style=colour)
            out.append("▱" * (9 - bar), style=C["faint"])
            out.append(f" {counts[k]}\n", style=C["dim"])
        out.append(f"{len(counts)}/{len(self.timeline.files)} files touched so far", style=C["dim"])
        self.query_one("#heat", Static).update(out)

    # ---------------------------------------------------------- actions

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        if event.pane.id == "tab-file":
            s = self.steps[self.current]
            self._file(s, self.mut_by_step.get(self.current))

    def on_resize(self, event: events.Resize) -> None:
        self._layout(event.size.width)

    def _layout(self, w: int) -> None:
        if not getattr(self, "side_forced", False):
            self.set_class(w < 150, "narrow")
        self.set_class(w < 110, "narrower")

    def action_hscroll(self, direction: int) -> None:
        tab = self.query_one("#tabs", TabbedContent).active
        pane_id = {"tab-diff": "#diffpane", "tab-file": "#filepane", "tab-out": "#outpane"}.get(tab, "#diffpane")
        pane = self.query_one(pane_id, ScrollableContainer)
        pane.scroll_to(x=max(0, pane.scroll_x + direction * 20), animate=True, duration=0.15)

    def action_zoom(self) -> None:
        self.toggle_class("zoom")
        self.notify("zoom on: diff/file fill the screen (z to return)" if self.has_class("zoom") else "zoom off",
                    timeout=2)

    def action_side(self) -> None:
        self.side_forced = True
        self.toggle_class("narrow")

    def action_tab(self, tab: str) -> None:
        self.query_one("#tabs", TabbedContent).active = tab

    def action_down(self) -> None:
        self.query_one("#timeline", DataTable).action_cursor_down()

    def action_up(self) -> None:
        self.query_one("#timeline", DataTable).action_cursor_up()

    def action_first(self) -> None:
        self.jump(self.rows[0] if self.rows else 0)

    def action_last(self) -> None:
        self.jump(self.rows[-1] if self.rows else 0)

    def _seek(self, pred, direction: int) -> None:
        pos = self.row_of.get(self.current, 0)
        rng = range(pos + 1, len(self.rows)) if direction > 0 else range(pos - 1, -1, -1)
        for r in rng:
            if pred(self.steps[self.rows[r]]):
                self.jump(self.rows[r])
                return

    def action_next_prompt(self) -> None:
        self._seek(lambda s: s.kind == "prompt", 1)

    def action_prev_prompt(self) -> None:
        self._seek(lambda s: s.kind == "prompt", -1)

    def action_next_edit(self) -> None:
        self._seek(lambda s: s.index in self.mut_by_step, 1)

    def action_prev_edit(self) -> None:
        self._seek(lambda s: s.index in self.mut_by_step, -1)

    def action_edits_only(self) -> None:
        self.mode = "default" if self.mode == "edits" else "edits"
        cur = self.current
        self._fill()
        self.jump(cur)

    def action_show_all(self) -> None:
        self.mode = "default" if self.mode == "all" else "all"
        cur = self.current
        self._fill()
        self.jump(cur)

    def action_play(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = None
        else:
            if self.row_of.get(self.current, 0) >= len(self.rows) - 1:
                self.jump(self.rows[0])
            self.timer = self.set_interval(0.9 / SPEEDS[self.speed_i], self._advance)
        self._topbar()

    def _advance(self) -> None:
        if not self.is_attached or self.app.screen is not self:
            return
        pos = self.row_of.get(self.current, 0)
        if pos >= len(self.rows) - 1:
            self.action_play()
            return
        self.jump(self.rows[pos + 1])

    def _retime(self) -> None:
        if self.timer is not None:
            self.timer.stop()
            self.timer = self.set_interval(0.9 / SPEEDS[self.speed_i], self._advance)
        self._topbar()
        self.notify(f"replay speed {SPEEDS[self.speed_i]}×", timeout=1.5)

    def action_faster(self) -> None:
        self.speed_i = min(self.speed_i + 1, len(SPEEDS) - 1)
        self._retime()

    def action_slower(self) -> None:
        self.speed_i = max(self.speed_i - 1, 0)
        self._retime()

    def action_export(self) -> None:
        j = self.state_after[self.current]
        root = self.root or self.session.cwd or os.getcwd()
        dest = tempfile.mkdtemp(prefix=f"rewind-{self.session.id[:8]}-step{self.current + 1}-")
        written = export_files(self.timeline, j, root, dest)
        self.notify(f"{len(written)} edited files as of step #{self.current + 1}\n{dest}", title="exported", timeout=10)

    # ---------------------------------------------------------- bisect

    def action_bisect(self) -> None:
        if self.bisecting:
            self.notify("A bisect is already running.", severity="warning")
            return
        if not self.timeline.mutations:
            self.notify("This session edited no files.", severity="warning")
            return
        self.app.push_screen(BisectPrompt(self.app.check or ""), self._start_bisect)  # type: ignore[attr-defined]

    def _start_bisect(self, cmd: str | None) -> None:
        if not cmd:
            return
        if self.timer is not None:
            self.action_play()
        self.bisecting = True
        self.culprit = None
        sc = self.query_one("#scrubber", Scrubber)
        sc.verdicts.clear()
        sc.culprit = None
        sc.window = (-1, len(self.steps) - 1)
        log = self.query_one("#blog", RichLog)
        log.clear()
        log.add_class("active")
        log.write(Text.assemble(("check  ", C["dim"]), (cmd, Style(color=C["fg"], bold=True))))
        self._topbar()
        self.run_bisect(cmd)

    def _state_step(self, j: int) -> int:
        return self.timeline.mutations[j - 1].step.index if j > 0 else 0

    @work(thread=True, exclusive=True, group="bisect")
    def run_bisect(self, cmd: str) -> None:
        from .bisect import bisect

        def log(text: Text) -> None:
            self.rw_app.call_from_thread(lambda: self.query_one("#blog", RichLog).write(text))

        try:
            root = choose_root(self.timeline, self.rw_app.root_override)  # type: ignore[attr-defined]
        except SandboxError as exc:
            self.rw_app.call_from_thread(self._bisect_failed, str(exc))
            return
        sb = Sandbox(self.timeline, root)
        log(Text(f"copying {R.short(root, 30)} into a sandbox…", style=C["dim"]))
        try:
            copied = sb.create()
            log(Text(f"{copied:,} files copied · {len(self.timeline.mutations)} edits to search", style=C["dim"]))
            res = bisect(self.timeline, sb, cmd, sb.cwd_for(self.session.cwd),
                         on_probe=lambda p, lo, hi: self.rw_app.call_from_thread(self._on_probe, p, lo, hi))
        except Exception as exc:  # noqa: BLE001 - surface any failure in the UI
            self.rw_app.call_from_thread(self._bisect_failed, f"{type(exc).__name__}: {exc}")
            return
        finally:
            sb.remove()
        self.rw_app.call_from_thread(self._bisect_done, res)

    def _on_probe(self, p, lo: int, hi: int) -> None:
        if not self.is_attached or self.app.screen is not self:
            return
        sc = self.query_one("#scrubber", Scrubber)
        idx = self._state_step(p.j)
        sc.verdicts[idx] = p.verdict
        sc.probe = idx
        sc.window = (self._state_step(lo), self._state_step(hi))
        sc.refresh()
        mark = {"good": ("✓ good", C["green"]), "bad": ("✗ bad ", C["red"]), "skip": ("∅ skip", C["yellow"]),
                "timeout": ("⧗ slow", C["yellow"])}[p.verdict]
        where = "before the session" if p.j == 0 else f"after #{self.timeline.mutations[p.j - 1].step.number}"
        self.query_one("#blog", RichLog).write(Text.assemble(
            (mark[0], Style(color=mark[1], bold=True)), (f"  state {p.j:>3}  {where}", C["fg"]),
            (f"  {p.seconds:.1f}s", C["dim"])))
        self.jump(idx)

    def _bisect_failed(self, message: str) -> None:
        self.bisecting = False
        self.query_one("#blog", RichLog).write(Text(message, style=C["red"]))
        self.notify(message, title="bisect failed", severity="error", timeout=10)
        self._topbar()

    def _bisect_done(self, res) -> None:
        if not self.is_attached or self.app.screen is not self:
            return
        self.bisecting = False
        sc = self.query_one("#scrubber", Scrubber)
        sc.probe = None
        log = self.query_one("#blog", RichLog)
        if res.culprit is None:
            log.write(Text(res.message, style=C["yellow"]))
            self.notify(res.message, title="no culprit", severity="warning", timeout=12)
            self._topbar()
            sc.refresh()
            return
        self.culprit = res.culprit
        self.culprit_runs = res.runs
        bad = next((x for x in reversed(res.probes) if x.j == res.bad and x.verdict == "bad"), None)
        self.culprit_output = "\n".join(bad.output.strip().splitlines()[-14:]) if bad else ""
        sc.culprit = res.culprit.step.index
        sc.refresh()
        log.write(Text.assemble(("FOUND ", Style(color=C["red"], bold=True)),
                                (f"#{res.culprit.step.number} {os.path.basename(res.culprit.path)}", C["fg"]),
                                (f" in {res.runs} runs", C["dim"])))
        if res.status == "flaky":
            log.write(Text(res.message, style=C["yellow"]))
        from .cli import _report_md

        report = Path(tempfile.gettempdir()) / f"rewind-{self.session.id[:8]}-bisect.md"
        report.write_text(_report_md(res, self.timeline, self.root, self.app.check or ""), encoding="utf-8")  # type: ignore[attr-defined]
        log.write(Text(f"report: {report}", style=C["dim"]))
        self.jump(res.culprit.step.index)
        self.notify(f"#{res.culprit.step.number} {R.rel(res.culprit.path, self.root)} · turn {res.culprit.step.turn}",
                    title="first bad edit found", severity="error", timeout=12)


def diff_table(m: Mutation, lexer: str, min_width: int = 80) -> Table:
    """Unified diff with both line numbers, syntax colour and full-width add/remove bands.

    Lines are never wrapped or cut: the table is as wide as the longest line
    (at least min_width) and the pane scrolls sideways.
    """
    before = m.before if isinstance(m.before, str) else ""
    after = m.after if isinstance(m.after, str) else ""
    longest = max((len(x.expandtabs(4)) for x in [*before.splitlines(), *after.splitlines()]), default=0)
    width = max(min_width, longest + 5 + 5 + 1 + 6)
    table = Table(show_header=False, box=None, width=width, padding=(0, 1), pad_edge=False)
    table.add_column(justify="right", style=C["dim"], width=5, no_wrap=True)
    table.add_column(justify="right", style=C["dim"], width=5, no_wrap=True)
    table.add_column(width=1, no_wrap=True)
    table.add_column(no_wrap=True, overflow="ignore", min_width=longest)
    if not known(m.before) or not known(m.after):
        table.add_row("", "", "", Text("(state not fully known; showing the recorded patch)", style=C["yellow"]))
        for line in m.diff().splitlines():
            style = C["green"] if line.startswith("+") else C["red"] if line.startswith("-") else C["dim"]
            table.add_row("", "", "", Text(line, style=style))
        return table
    syn = Syntax("", lexer, theme="monokai", background_color="default")
    a, b = before.splitlines(), after.splitlines()
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    for group in sm.get_grouped_opcodes(4):
        i1, j1 = group[0][1], group[0][3]
        table.add_row("", "", "", Text(f"@@ −{i1 + 1} +{j1 + 1} @@", style=Style(color=C["blue"], bold=True)),
                      style=Style(bgcolor="#1f2335"))
        for op, x1, x2, y1, y2 in group:
            if op == "equal":
                for k in range(x2 - x1):
                    table.add_row(str(x1 + k + 1), str(y1 + k + 1), " ", _hl(syn, a[x1 + k]))
                continue
            for k in range(x1, x2):
                table.add_row(str(k + 1), "", Text("−", style=C["red"]), _hl(syn, a[k]),
                              style=Style(bgcolor=C["del_bg"]))
            for k in range(y1, y2):
                table.add_row("", str(k + 1), Text("+", style=C["green"]), _hl(syn, b[k]),
                              style=Style(bgcolor=C["add_bg"]))
    if not sm.get_opcodes() or all(op == "equal" for op, *_ in sm.get_opcodes()):
        table.add_row("", "", "", Text("(no textual change)", style=C["dim"]))
    return table


def _hl(syn: Syntax, line: str) -> Text:
    t = syn.highlight(line.expandtabs(4))
    t.rstrip()
    return t


# --------------------------------------------------------------------- app


class RewindApp(App):
    TITLE = "rewind"
    ENABLE_COMMAND_PALETTE = True

    def __init__(self, loader, check: str | None = None, root_override: str | None = None):
        super().__init__()
        self.loader = loader
        self.check = check
        self.root_override = root_override

    def on_mount(self) -> None:
        try:
            self.theme = "tokyo-night"
        except Exception:  # noqa: BLE001 - older Textual without themes
            pass
        self.push_screen(BootScreen(self.loader))


def make_loader(path: Path, home: Path, use_git: bool = True):
    def loader(stage, facts, progress):
        stage("reading the transcript…")
        session = load_session(path, home)
        edits = sum(1 for s in session.steps if s.is_mutation_tool and not s.error)
        facts(Text.assemble(
            (f"{len(session.steps):,}", Style(color=C["fg"], bold=True)), (" steps   ", C["dim"]),
            (f"{len(session.prompts)}", Style(color=C["cyan"], bold=True)), (" prompts   ", C["dim"]),
            (f"{edits}", Style(color=C["yellow"], bold=True)), (" edits   ", C["dim"]),
            (f"{len(session.agents)}", Style(color=C["blue"], bold=True)), (" subagents\n", C["dim"]),
            (R.short(session.title, 60), C["fg"])))
        stage("rebuilding every version of every file…")
        timeline = build_timeline(session, use_git=use_git, progress=progress)
        good, total = timeline.fidelity()
        stage(f"rebuilt {good}/{total} file states exactly · {len(timeline.external_changes)} outside changes spotted")
        return session, timeline

    return loader


def run_tui(path: Path, home: Path, use_git: bool = True, check: str | None = None, root: str | None = None) -> None:
    RewindApp(make_loader(path, home, use_git), check=check, root_override=root).run()
