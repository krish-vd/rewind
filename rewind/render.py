"""Terminal rendering with rich."""

from __future__ import annotations

import os
from datetime import datetime

from rich.console import Console, Group
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .model import Session, Step
from .reconstruct import Mutation, Timeline, known

ICONS = {
    "prompt": ("❯", "bold cyan"),
    "edit": ("✎", "yellow"),
    "write": ("✚", "green"),
    "shell": ("$", "magenta"),
    "read": ("◌", "grey50"),
    "tool": ("·", "grey50"),
    "compact": ("⇣", "red"),
}
QUALITY_STYLE = {"exact": "green", "derived": "green", "inferred": "yellow", "approx": "yellow", "unknown": "red"}
SPARKS = "▁▂▃▄▅▆▇█"


def console() -> Console:
    return Console(highlight=False)


def when(ts: str, with_date: bool = False) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return ts
    return dt.strftime("%Y-%m-%d %H:%M" if with_date else "%H:%M:%S")


def short(text: str, n: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k" if n >= 10_000 else f"{n / 1000:.1f}k"
    return str(n)


def sparkline(values: list[int], width: int = 60) -> str:
    if not values:
        return ""
    if len(values) > width:
        size = len(values) / width
        values = [max(values[int(i * size) : max(int((i + 1) * size), int(i * size) + 1)]) for i in range(width)]
    hi = max(values) or 1
    return "".join(SPARKS[min(len(SPARKS) - 1, int(v / hi * (len(SPARKS) - 1)))] for v in values)


def rel(path: str, root: str | None) -> str:
    if not path:
        return ""
    if root:
        try:
            r = os.path.relpath(path, root)
            if not r.startswith(".."):
                return r.replace("\\", "/")
        except ValueError:
            pass
    return path


def step_line(step: Step, root: str | None = None, width: int = 90) -> Text:
    icon, style = ICONS.get(step.kind, ("·", "grey50"))
    t = Text()
    t.append(f"{step.number:>5} ", style="grey50")
    t.append(f"{icon} ", style=style)
    if step.kind == "prompt":
        t.append(short(step.text, width), style="bold")
    elif step.kind in ("edit", "write", "read"):
        t.append(f"{step.tool} ", style=style)
        t.append(rel(step.path or step.text, root))
    elif step.kind == "shell":
        t.append(short(step.text, width), style="magenta")
    elif step.kind == "compact":
        t.append(step.text, style="red")
    else:
        t.append(f"{step.tool} ", style="grey50")
        t.append(short(step.text, width - 10), style="grey50")
    if step.error:
        t.append("  failed", style="red")
    if step.agent != "main":
        t.append(f"  [{step.agent[:7]}]", style="blue")
    return t


def diff_view(m: Mutation, context: int = 3) -> Syntax:
    return Syntax(m.diff(context) or "(no textual change)", "diff", theme="ansi_dark", background_color="default",
                  word_wrap=True)


def header(session: Session, timeline: Timeline | None = None) -> Panel:
    t = Text()
    t.append(session.title or session.id, style="bold")
    t.append(f"\n{session.id}", style="grey50")
    t.append(f"\n{when(session.started, True)} → {when(session.ended)}", style="grey70")
    if session.branch:
        t.append(f"   branch {session.branch}", style="grey70")
    prompts = len(session.prompts)
    edits = sum(1 for s in session.steps if s.is_mutation_tool and not s.error)
    shells = sum(1 for s in session.steps if s.kind == "shell")
    t.append(f"\n{prompts} prompts · {edits} edits · {shells} shell commands · {len(session.steps)} steps")
    if session.agents:
        t.append(f" · {len(session.agents)} subagents")
    if timeline is not None:
        good, total = timeline.fidelity()
        pct = good / total if total else 1
        style = "green" if pct > 0.95 else "yellow" if pct > 0.8 else "red"
        t.append("\nreconstruction ")
        t.append(f"{pct:.0%}", style=f"bold {style}")
        t.append(f" of {total} file states exact · {len(timeline.files)} files", style="grey70")
        if timeline.external_changes:
            t.append(f" · {len(timeline.external_changes)} outside changes detected", style="yellow")
    ctx = [c.usage.context for c in session.calls if c.agent == "main"]
    if ctx:
        t.append("\ncontext ")
        t.append(sparkline(ctx, 50), style="cyan")
        t.append(f" peak {tokens(max(ctx))}", style="grey70")
    return Panel(t, border_style="cyan", title="[bold cyan]rewind[/]", title_align="left")


def timeline_view(session: Session, root: str | None, show_all: bool) -> Group:
    rows = []
    for s in session.steps:
        if not show_all and s.kind in ("read", "tool") and not s.error:
            continue
        if s.kind == "prompt":
            rows.append(Rule(style="grey23"))
            rows.append(Text(f"  turn {s.turn} · {when(s.timestamp)}", style="grey50"))
        rows.append(step_line(s, root))
    return Group(*rows)


def files_table(timeline: Timeline, root: str | None) -> Table:
    t = Table(box=None, header_style="bold", pad_edge=False)
    t.add_column("file")
    t.add_column("edits", justify="right")
    t.add_column("states", justify="left")
    t.add_column("start", justify="left")
    t.add_column("vs disk now")
    for fh in sorted(timeline.files.values(), key=lambda f: f.path):
        states = Text()
        for q in [fh.mutations[0].before_q] + [m.after_q for m in fh.mutations]:
            states.append("■", style=QUALITY_STYLE.get(q, "red"))
        base = fh.baseline
        start = "new file" if base is None else ("unknown" if not known(base) else f"{len(base.splitlines())} lines")
        md = fh.matches_disk
        disk = Text("same", style="green") if md else Text("changed since", style="yellow") if md is False else Text("?", style="red")
        if fh.disk is None and known(fh.final) and fh.final is not None:
            disk = Text("deleted since", style="yellow")
        t.add_row(rel(fh.path, root), str(len(fh.mutations)), states, start, disk)
    return t


def legend() -> Text:
    t = Text("  ")
    for q, label in (("exact", "exact"), ("inferred", "inferred from disk"), ("unknown", "unknown")):
        t.append("■", style=QUALITY_STYLE[q])
        t.append(f" {label}   ", style="grey50")
    return t


def external_view(timeline: Timeline, root: str | None) -> Group:
    rows = []
    for e in timeline.external_changes:
        t = Text()
        t.append("  ⚠ ", style="yellow")
        t.append(rel(e.path, root), style="bold")
        t.append(f"  {when(e.timestamp)}  {e.detail}", style="grey70")
        rows.append(t)
        for s in e.suspects[:2]:
            rows.append(Text(f"      maybe #{s.number} $ {short(s.text, 80)}", style="magenta"))
    return Group(*rows)


def bisect_bar(n: int, verdicts: dict[int, str], lo: int, hi: int, width: int = 64) -> Text:
    states = n + 1
    buckets = min(states, width)
    t = Text()
    for b in range(buckets):
        a = int(b * states / buckets)
        z = max(int((b + 1) * states / buckets), a + 1)
        vs = [verdicts[k] for k in range(a, z) if k in verdicts]
        if "bad" in vs:
            t.append("█", style="red")
        elif "good" in vs:
            t.append("█", style="green")
        elif "skip" in vs:
            t.append("█", style="yellow")
        elif any(lo < k < hi for k in range(a, z)):
            t.append("▒", style="bright_white")
        else:
            t.append("░", style="grey30")
    return t


def mutation_title(m: Mutation, root: str | None) -> Text:
    t = Text()
    t.append(f"#{m.step.number} ", style="bold")
    t.append(f"{m.step.tool} ", style=ICONS[m.step.kind][1])
    t.append(rel(m.path, root), style="bold")
    added, removed = m.changed_lines
    t.append(f"   +{added} −{removed}", style="grey70")
    t.append(f"   turn {m.step.turn} · {when(m.step.effective_timestamp, True)}", style="grey50")
    if m.step.agent != "main":
        t.append(f" · subagent {m.step.agent[:7]}", style="blue")
    return t


def quote(label: str, text: str, style: str, limit: int = 600) -> Group:
    body = text.strip()
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    return Group(Text(label, style="grey50"), Padding(Text(body, style=style), (0, 0, 0, 2)))
