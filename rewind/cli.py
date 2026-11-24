"""rewind: a flight recorder and debugger for AI coding agent sessions."""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from rich.console import Group
from rich.padding import Padding
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

from . import __version__
from . import render as R
from .bisect import BisectResult, Probe, bisect
from .reconstruct import Timeline, build_timeline
from .sandbox import Sandbox, SandboxError, choose_root, export_files
from .transcript import (
    SessionNotFound, claude_home, load_session, project_dir_for, resolve_session, session_files, summarize,
)

con = R.console()


# ----------------------------------------------------------------- helpers


def _load(args) -> tuple:
    home = claude_home(args.claude_home)
    path = resolve_session(getattr(args, "session", None), args.project, home)
    with con.status("Reading the session…", spinner="dots"):
        session = load_session(path, home)
        timeline = build_timeline(session, use_git=not args.no_git)
    return home, session, timeline


def _root_for_display(session, timeline) -> str | None:
    try:
        return choose_root(timeline)
    except SandboxError:
        return session.cwd or None


def _state_from_step(timeline: Timeline, step: int | None, state: int | None, default: int) -> int:
    if state is not None:
        return max(0, min(state, len(timeline.mutations)))
    if step is not None:
        return timeline.mutations_through_step(step - 1)
    return default


# ---------------------------------------------------------------- commands


def cmd_sessions(args) -> int:
    home = claude_home(args.claude_home)
    pdir = project_dir_for(args.project, home)
    if pdir is None:
        con.print(f"[red]No Claude Code sessions recorded for {os.path.abspath(args.project)}[/]")
        return 1
    files = session_files(pdir)[: args.limit]
    t = Table(box=None, header_style="bold", pad_edge=False)
    for col, kw in (("#", {"justify": "right"}), ("id", {}), ("when", {}), ("prompts", {"justify": "right"}),
                    ("edits", {"justify": "right"}), ("files", {"justify": "right"}), ("title", {})):
        t.add_column(col, **kw)
    with con.status("Scanning sessions…"):
        rows = [summarize(f) for f in files]
    for i, s in enumerate(rows, 1):
        t.add_row(str(i), s.id[:8], R.when(s.started, True), str(s.prompts), str(s.edits), str(s.files),
                  R.short(s.title or s.first_prompt, 60), style=None if s.edits else "grey50")
    con.print(t)
    con.print(Text("\nPick one by number or id prefix: rewind show 3 · rewind bisect 3 -- <check>", style="grey50"))
    return 0


def cmd_show(args) -> int:
    _home, session, timeline = _load(args)
    root = _root_for_display(session, timeline)
    con.print(R.header(session, timeline))
    con.print(R.timeline_view(session, root, args.all))
    if not args.all:
        con.print(Text("\n(reads and other tools hidden; --all shows them)", style="grey50"))
    return 0


def cmd_files(args) -> int:
    _home, session, timeline = _load(args)
    root = _root_for_display(session, timeline)
    con.print(R.header(session, timeline))
    if not timeline.files:
        con.print("This session edited no files.")
        return 0
    con.print(R.files_table(timeline, root))
    con.print(R.legend())
    if timeline.external_changes:
        con.print(Rule("changed outside the agent's edit tools", style="yellow"))
        con.print(R.external_view(timeline, root))
    return 0


def cmd_step(args) -> int:
    _home, session, timeline = _load(args)
    root = _root_for_display(session, timeline)
    if not 1 <= args.step <= len(session.steps):
        con.print(f"[red]There is no step #{args.step} (the session has {len(session.steps)}).[/]")
        return 1
    step = session.steps[args.step - 1]
    m = next((m for m in timeline.mutations if m.step is step), None)
    parts = [R.step_line(step, root, 200)]
    p = session.prompt_for_turn(step.turn)
    if p and p is not step:
        parts.append(R.quote(f"turn {step.turn}, you asked:", p.text, "cyan"))
    if step.why:
        parts.append(R.quote("the agent said just before:", step.why, "white"))
    if m is not None:
        parts.append(Text(f"state quality: before {m.before_q}, after {m.after_q}", style="grey50"))
        parts.append(R.diff_view(m, args.context))
    elif step.output:
        parts.append(R.quote("output:", step.output[-2000:], "grey70", 2000))
    con.print(Panel(Group(*parts), border_style="cyan", title=f"step #{step.number}", title_align="left"))
    return 0


def cmd_checkout(args) -> int:
    _home, session, timeline = _load(args)
    j = _state_from_step(timeline, args.step, args.state, len(timeline.mutations))
    try:
        root = choose_root(timeline, args.root)
    except SandboxError as exc:
        con.print(f"[red]{exc}[/]")
        return 1
    out = Path(args.out)
    if out.exists() and any(out.iterdir()):
        con.print(f"[red]{out} is not empty; choose a new folder.[/]")
        return 1
    if args.full:
        sb = Sandbox(timeline, root, dest=str(out))
        with con.status(f"Copying {root}…"):
            sb.create()
            weak = sb.apply(j)
        con.print(f"[green]Full project at state {j}/{len(timeline.mutations)} written to[/] {out}")
        for path, q in weak:
            con.print(f"  [yellow]{q}[/] {R.rel(path, root)}")
    else:
        written = export_files(timeline, j, root, str(out))
        con.print(f"[green]{len(written)} edited files at state {j}/{len(timeline.mutations)} written to[/] {out}")
    return 0


def _plan_panel(session, timeline, sandbox: Sandbox, cmd: str, good: int, bad: int, confirm: bool) -> Panel:
    n = bad - good
    runs = 2 + (math.ceil(math.log2(n)) if n > 1 else 0) + (2 if confirm else 0)
    t = Text()
    t.append("check    ", style="grey50")
    t.append(cmd + "\n", style="bold")
    t.append("project  ", style="grey50")
    t.append(f"{sandbox.root}\n")
    t.append("search   ", style="grey50")
    t.append(f"{n} edits across {len(timeline.files)} files · about {runs} runs of the check\n")
    t.append("where    ", style="grey50")
    t.append(f"a throwaway copy in {sandbox.dir} (your files are not touched)")
    shells = sum(1 for s in session.steps if s.kind == "shell")
    notes = []
    if sandbox.outside:
        notes.append(f"{len(sandbox.outside)} edited files are outside the project folder and stay as they are now")
    if sandbox.held:
        notes.append(f"held at today's version: {', '.join(sandbox.held[:4])}{'…' if len(sandbox.held) > 4 else ''}")
    good_states, total = timeline.fidelity()
    if good_states < total:
        notes.append(f"{total - good_states} of {total} file states are not exact (see `rewind files`)")
    if shells:
        notes.append(f"{shells} shell commands are not replayed; files the session never edited stay as they are today")
    for note in notes:
        t.append("\n· " + note, style="grey50")
    return Panel(t, title="[bold]bisect[/]", title_align="left", border_style="cyan")


def _probe_printer(timeline: Timeline, root: str | None):
    verdicts: dict[int, str] = {}
    n = len(timeline.mutations)
    count = [0]

    def show(p: Probe, lo: int, hi: int) -> None:
        count[0] += 1
        verdicts[p.j] = p.verdict
        style = {"good": "green", "bad": "red", "skip": "yellow", "timeout": "yellow"}[p.verdict]
        mark = {"good": "✓ good", "bad": "✗ bad ", "skip": "∅ skip", "timeout": "⧗ slow"}[p.verdict]
        t = Text()
        t.append(f"  run {count[0]:>2}  ", style="grey50")
        t.append_text(R.bisect_bar(n, verdicts, lo, hi, 40))
        t.append(f"  {mark}", style=f"bold {style}")
        t.append(f"  state {p.j:>3}/{n}", style="grey70")
        if p.j == 0:
            t.append("  before the session", style="grey50")
        else:
            m = timeline.mutations[p.j - 1]
            t.append(f"  after #{m.step.number} {R.short(R.rel(m.path, root), 34)}", style="grey50")
        t.append(f"  {p.seconds:.1f}s", style="grey37")
        con.print(t)

    return show


def _result_panel(res: BisectResult, timeline: Timeline, root: str | None, cmd: str) -> Panel:
    if res.status in ("not-reproduced", "bad-from-start") or (res.status == "ambiguous" and not res.skipped_range):
        return Panel(Text(res.message), border_style="yellow", title="[bold yellow]no culprit[/]", title_align="left")
    if res.status == "ambiguous":
        rows = [Text(res.message)] + [R.mutation_title(m, root) for m in res.skipped_range]
        return Panel(Group(*rows), border_style="yellow", title="[bold yellow]narrowed down[/]", title_align="left")
    m = res.culprit
    s = timeline.session
    parts = [R.mutation_title(m, root), Text("")]
    p = s.prompt_for_turn(m.step.turn)
    if p:
        parts.append(R.quote(f"turn {m.step.turn}, you asked:", p.text, "cyan", 500))
    if m.step.why:
        parts.append(R.quote("the agent said:", m.step.why, "white", 500))
    parts.append(Text(""))
    parts.append(R.diff_view(m))
    bad_probe = next((x for x in reversed(res.probes) if x.j == res.bad and x.verdict == "bad"), None)
    if bad_probe and bad_probe.output.strip():
        tail = "\n".join(bad_probe.output.strip().splitlines()[-12:])
        parts.append(R.quote("check output right after this edit:", tail, "red", 1500))
    later = [x for x in timeline.mutations[m.seq + 1 :] if x.key == m.key]
    parts.append(Text(""))
    summary = Text(f"{res.runs} runs of the check · last good state {res.good} · first bad state {res.bad}", style="grey50")
    if later:
        summary.append(f" · {len(later)} later edits touched the same file", style="grey50")
    parts.append(summary)
    weak = {path for x in res.probes if x.j in (res.good, res.bad) for path, _q in x.weak}
    if weak:
        parts.append(Text(f"⚠ {len(weak)} files at the boundary are not exact: " + ", ".join(R.rel(w, root) for w in list(weak)[:3]),
                          style="yellow"))
    title = "[bold red]first bad edit[/]" if res.status == "found" else "[bold yellow]suspect (flaky check)[/]"
    if res.status == "flaky":
        parts.insert(0, Text(res.message + "\n", style="yellow"))
    return Panel(Group(*parts), border_style="red" if res.status == "found" else "yellow", title=title, title_align="left")


def _report_md(res: BisectResult, timeline: Timeline, root: str | None, cmd: str) -> str:
    m = res.culprit
    s = timeline.session
    p = s.prompt_for_turn(m.step.turn)
    bad_probe = next((x for x in reversed(res.probes) if x.j == res.bad and x.verdict == "bad"), None)
    out = "\n".join(bad_probe.output.strip().splitlines()[-30:]) if bad_probe else ""
    path = R.rel(m.path, root)
    lines = [
        f"# rewind bisect: first bad edit is step #{m.step.number}",
        "",
        f"- Session: {s.title} (`{s.id}`), {R.when(s.started, True)}",
        f"- Check: `{cmd}`",
        f"- Edit: #{m.step.number} {m.step.tool} `{path}`, turn {m.step.turn}, {R.when(m.step.effective_timestamp, True)}",
        f"- Runs: {res.runs}; last good state {res.good}, first bad state {res.bad} of {len(timeline.mutations)}",
        "",
        "## What was asked",
        "",
        "> " + (p.text if p else "").replace("\n", "\n> "),
        "",
        "## What the agent said before the edit",
        "",
        "> " + (m.step.why or "(nothing)").replace("\n", "\n> "),
        "",
        "## The edit",
        "",
        "```diff",
        m.diff().rstrip(),
        "```",
        "",
        "## Check output right after the edit",
        "",
        "```",
        out,
        "```",
        "",
        "## Prompt to hand to your agent",
        "",
        "```",
        f"`{cmd}` started failing at one specific edit, found by bisecting an earlier agent session.",
        f"The edit was made to {path} while working on: \"{R.short(p.text if p else '', 300)}\".",
        f"The agent's stated intent was: \"{R.short(m.step.why, 300)}\".",
        "",
        "Here is the diff of that edit:",
        "",
        m.diff().rstrip(),
        "",
        "And the check output right after it:",
        "",
        out,
        "",
        "Fix the regression while keeping the intent of that change. Explain the root cause first.",
        "```",
        "",
    ]
    return "\n".join(lines)


def cmd_bisect(args) -> int:
    cmd = args.cmd
    if not cmd:
        con.print("[red]Give the check to run: rewind bisect [SESSION] -- pytest -q tests/test_x.py[/]")
        return 2
    _home, session, timeline = _load(args)
    if not timeline.mutations:
        con.print("[yellow]This session made no file edits, so there is nothing to bisect.[/]")
        return 1
    try:
        root = choose_root(timeline, args.root)
    except SandboxError as exc:
        con.print(f"[red]{exc}[/]")
        return 1
    good = _state_from_step(timeline, args.good_step, args.good_state, 0)
    bad = _state_from_step(timeline, args.bad_step, args.bad_state, len(timeline.mutations))
    if not good < bad:
        con.print(f"[red]The good state ({good}) must come before the bad state ({bad}).[/]")
        return 2
    sandbox = Sandbox(timeline, root, hold=args.hold)
    con.print(R.header(session, timeline))
    con.print(_plan_panel(session, timeline, sandbox, cmd, good, bad, not args.no_confirm))
    try:
        with con.status(f"Copying {root} into the sandbox…"):
            copied = sandbox.create()
        con.print(Text(f"  copied {copied:,} files", style="grey50"))
        run_dir = sandbox.cwd_for(session.cwd)
        res = bisect(timeline, sandbox, cmd, run_dir, good=good, bad=bad, timeout=args.timeout,
                     confirm=not args.no_confirm, on_probe=_probe_printer(timeline, root))
        con.print()
        con.print(_result_panel(res, timeline, root, cmd))
        if res.culprit is not None:
            report = Path(args.report) if args.report else Path(tempfile.gettempdir()) / f"rewind-{session.id[:8]}-bisect.md"
            report.write_text(_report_md(res, timeline, root, cmd), encoding="utf-8")
            con.print(Text(f"report with a ready-to-paste fix prompt: {report}", style="grey50"))
            con.print(Text(f"last good state: rewind checkout {session.id[:8]} --state {res.good} --out <dir> --full",
                           style="grey50"))
        return 0 if res.status == "found" else 3
    finally:
        if args.keep:
            con.print(Text(f"sandbox kept at {sandbox.dir}", style="grey50"))
        else:
            sandbox.remove()


def cmd_blame(args) -> int:
    from .blame import blame, timelines_touching

    home = claude_home(args.claude_home)
    target = os.path.abspath(args.file)
    pdir = project_dir_for(args.project, home)
    if pdir is None:
        con.print(f"[red]No sessions recorded for {os.path.abspath(args.project)}; use -p.[/]")
        return 1
    with con.status("Replaying every session that touched the file…"):
        tls = timelines_touching(target, pdir, home, args.sessions)
        lines = blame(target, tls)
    if not tls:
        con.print("[yellow]No recorded agent session edited this file.[/]")
    lo, hi = 1, len(lines)
    if args.L:
        a, _, b = args.L.partition(",")
        lo = int(a)
        hi = int(b) if b else lo
    palette = ["cyan", "magenta", "yellow", "green", "blue", "bright_red", "bright_cyan", "bright_magenta"]
    colours: dict = {}
    t = Table(box=None, show_header=False, pad_edge=False)
    t.add_column(justify="right", style="grey50")
    t.add_column(no_wrap=True)
    t.add_column(overflow="fold")
    prev = None
    for ln in lines[lo - 1 : hi]:
        src = ln.source
        if src not in colours:
            colours[src] = "grey50" if src.kind != "agent" else palette[len([k for k in colours if k.kind == "agent"]) % len(palette)]
        label = src.label if src != prev else ""
        prev = src
        t.add_row(str(ln.number), Text(f"{label:<18}", style=colours[src]), Text(ln.text, style="default"))
    con.print(t)
    used = []
    for ln in lines[lo - 1 : hi]:
        if ln.source.kind == "agent" and ln.source not in used:
            used.append(ln.source)
    if used:
        con.print(Rule("who asked for these lines", style="grey23"))
        for src in used:
            row = Text()
            row.append(f"{src.label:<18}", style=colours[src])
            row.append(f"{R.when(src.timestamp, True)}  ", style="grey50")
            row.append(R.short(src.prompt, 100), style="cyan")
            con.print(row)
            if src.why:
                con.print(Text(" " * 18 + R.short(src.why, 110), style="grey70"))
    return 0


def cmd_context(args) -> int:
    from .context import analyse, est_tokens

    _home, session, timeline = _load(args)
    rep = analyse(session)
    con.print(R.header(session, timeline))
    if not rep.calls:
        con.print("No model calls recorded.")
        return 0
    ctx = [c.usage.context for c in rep.calls]
    con.print(Text("context per call", style="bold"))
    con.print(Text("  " + R.sparkline(ctx, 100), style="cyan"))
    con.print(Text(f"  peak {R.tokens(rep.peak)} · {len(rep.calls)} calls · {R.tokens(rep.processed)} input tokens "
                   f"processed · {rep.cache_ratio:.0%} served from cache · {R.tokens(rep.output)} output"
                   + (f" · {rep.subagent_calls} subagent calls" if rep.subagent_calls else ""), style="grey70"))
    for c in rep.compactions:
        con.print(Text(f"  ⇣ #{c.number} {c.text}", style="red"))

    t = Table(title="by turn", title_justify="left", box=None, header_style="bold", pad_edge=False)
    for col in ("turn", "calls", "peak", "processed", "cache", "out", "prompt"):
        t.add_column(col, justify="right" if col not in ("prompt",) else "left")
    for ts in rep.turns:
        cache = ts.cache_read / ts.processed if ts.processed else 0
        t.add_row(str(ts.turn), str(ts.calls), R.tokens(ts.peak), R.tokens(ts.processed), f"{cache:.0%}",
                  R.tokens(ts.output), R.short(ts.prompt, 60))
    con.print()
    con.print(t)

    if rep.hogs:
        h = Table(title="biggest tool outputs (≈ tokens)", title_justify="left", box=None, header_style="bold",
                  pad_edge=False)
        h.add_column("≈tok", justify="right")
        h.add_column("×", justify="right")
        h.add_column("")
        h.add_column("what")
        top = rep.hogs[0][1] or 1
        for label, chars, count in rep.hogs:
            bar = "█" * max(1, int(chars / top * 24))
            h.add_row(R.tokens(est_tokens(chars)), str(count), Text(bar, style="magenta"), R.short(label, 70))
        con.print()
        con.print(h)
    if rep.rereads:
        con.print()
        con.print(Text("read more than once", style="bold"))
        for path, n, chars in rep.rereads:
            con.print(Text(f"  {n}×  ≈{R.tokens(est_tokens(chars))}  {R.rel(path, session.cwd)}", style="grey70"))
    if rep.jumps:
        con.print()
        con.print(Text("largest single jumps", style="bold"))
        for j in rep.jumps[:5]:
            cause = f"#{j.cause.number} {R.short(R.step_line(j.cause).plain.strip(), 70)}" if j.cause else "model output / prompt"
            con.print(Text(f"  +{R.tokens(j.delta):>6}  turn {j.after.turn:<3} {cause}", style="grey70"))
    return 0


def cmd_tui(args) -> int:
    from .tui import run_tui

    home = claude_home(args.claude_home)
    path = resolve_session(args.session, args.project, home)
    run_tui(path, home, use_git=not args.no_git, check=args.check, root=args.root)
    return 0


def cmd_demo(args) -> int:
    from .demo import build_demo

    base = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="rewind-demo-"))
    home, project, transcript = build_demo(base)
    check = subprocess.list2cmdline([sys.executable, "check_discount.py"]) if os.name == "nt" else f"'{sys.executable}' check_discount.py"
    if args.tui:
        from .tui import run_tui

        run_tui(transcript, home, use_git=False, check=check)
        return 0
    con.print(Panel(Text.assemble(
        ("A scripted demo session ", "bold"), ("(synthetic, in Claude Code's transcript format)\n", "grey50"),
        "An agent adds discount codes, moves money maths to integer cents, adds bulk pricing,\n"
        "tidies up and edits the README. Its own test run passed. The regression check already\n"
        "in the repo does not:  ", ("python check_discount.py", "bold"),
        ("\n\nproject   ", "grey50"), str(project), ("\nsessions  ", "grey50"), str(home)),
        border_style="cyan", title="[bold cyan]rewind demo[/]", title_align="left"))
    ns = argparse.Namespace(
        claude_home=str(home), project=str(project), session="latest", no_git=True, cmd=check, root=None, hold=[],
        good_step=None, bad_step=None, good_state=None, bad_state=None, timeout=60, keep=False, no_confirm=False,
        report=str(base / "bisect-report.md"),
    )
    con.print()
    rc = cmd_bisect(ns)
    con.print(Text("\nExplore the same session:", style="bold"))
    con.print(Text(f'  cd "{project}"', style="grey70"), soft_wrap=True)
    for sub_cmd in ("tui", "files", "context", "blame shop/pricing.py"):
        con.print(Text(f'  rewind {sub_cmd} --claude-home "{home}"', style="grey70"), soft_wrap=True)
    return 0 if rc == 0 else rc


# -------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", default=os.getcwd(), help="project folder the sessions ran in (default: here)")
    common.add_argument("--claude-home", help="Claude Code config folder (default: ~/.claude or $CLAUDE_CONFIG_DIR)")
    common.add_argument("--no-git", action="store_true", help="don't use git history to fill gaps")

    p = argparse.ArgumentParser(
        prog="rewind",
        description="A flight recorder and debugger for AI coding agent sessions. Rebuilds the code at every step "
                    "of a Claude Code session from its transcript, finds the edit that broke a check, and shows "
                    "which prompt wrote which line.",
    )
    p.add_argument("--version", action="version", version=f"rewind {__version__} — engineered by Krish Desai")
    p.epilog = "Engineered by Krish Desai."
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("sessions", parents=[common], help="list recorded sessions for this project")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.set_defaults(func=cmd_sessions)

    for name, func, helptext in (
        ("show", cmd_show, "print a session's timeline"),
        ("files", cmd_files, "files a session edited, reconstruction quality, outside changes"),
        ("context", cmd_context, "where the context window went"),
        ("tui", cmd_tui, "explore a session interactively"),
    ):
        sp = sub.add_parser(name, parents=[common], help=helptext)
        sp.add_argument("session", nargs="?", default="latest", help="latest (default), list number, id prefix or .jsonl path")
        if name == "show":
            sp.add_argument("--all", action="store_true", help="include reads and other tools")
        if name == "tui":
            sp.add_argument("--check", help="check command to prefill for bisect (press b)")
            sp.add_argument("--root", help="project root for bisect/export")
        sp.set_defaults(func=func)

    st = sub.add_parser("step", parents=[common], help="details and diff of one step")
    st.add_argument("session", nargs="?", default="latest")
    st.add_argument("step", type=int)
    st.add_argument("-U", "--context", type=int, default=3, help="diff context lines")
    st.set_defaults(func=cmd_step)

    c = sub.add_parser("checkout", parents=[common], help="write the code as it was at a step")
    c.add_argument("session", nargs="?", default="latest")
    c.add_argument("--step", type=int, help="state right after this step number")
    c.add_argument("--state", type=int, help="state after the first N edits (0 = before the session)")
    c.add_argument("--out", required=True, help="new folder to write into")
    c.add_argument("--full", action="store_true", help="copy the whole project, not just the edited files")
    c.add_argument("--root", help="project root (default: git repo holding the edited files)")
    c.set_defaults(func=cmd_checkout)

    b = sub.add_parser("bisect", parents=[common], help="find the edit that broke a check: rewind bisect [SESSION] -- <check>")
    b.add_argument("session", nargs="?", default="latest")
    b.add_argument("--cmd", help="check to run (exit 0 good, 125 skip, else bad); or put it after --")
    b.add_argument("--root", help="project root to copy (default: git repo holding the edited files)")
    b.add_argument("--hold", action="append", default=[], help="path or glob to keep at today's version in every state (e.g. tests/)")
    b.add_argument("--good-step", type=int, help="a step number known to be good")
    b.add_argument("--bad-step", type=int, help="a step number known to be bad")
    b.add_argument("--good-state", type=int, help=argparse.SUPPRESS)
    b.add_argument("--bad-state", type=int, help=argparse.SUPPRESS)
    b.add_argument("--timeout", type=float, default=600, help="seconds per run (default 600)")
    b.add_argument("--no-confirm", action="store_true", help="skip re-running the boundary to rule out flakiness")
    b.add_argument("--keep", action="store_true", help="keep the sandbox folder afterwards")
    b.add_argument("--report", help="where to write the markdown report")
    b.set_defaults(func=cmd_bisect)

    bl = sub.add_parser("blame", parents=[common], help="which prompt wrote each line of a file")
    bl.add_argument("file")
    bl.add_argument("-L", help="line or range, e.g. 40,60")
    bl.add_argument("--sessions", type=int, help="only look at the N most recent sessions")
    bl.set_defaults(func=cmd_blame)

    d = sub.add_parser("demo", help="build a demo session and bisect it")
    d.add_argument("--dir", help="where to build it (default: a temp folder)")
    d.add_argument("--tui", action="store_true", help="open the demo in the interactive cockpit instead")
    d.set_defaults(func=cmd_demo)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    trailing = None
    if "--" in argv:
        i = argv.index("--")
        trailing = argv[i + 1 :]
        argv = argv[:i]
    parser = build_parser()
    if not argv:
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    if trailing:
        if getattr(args, "command", None) != "bisect":
            parser.error("'--' is only used with bisect")
        args.cmd = subprocess.list2cmdline(trailing) if os.name == "nt" else " ".join(
            __import__("shlex").quote(a) for a in trailing)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except SessionNotFound as exc:
        con.print(f"[red]{exc}[/]")
        return 1
    except KeyboardInterrupt:
        con.print("\n[yellow]stopped[/]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
