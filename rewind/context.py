"""Where a session's context window went.

Sizes of tool output are estimated from characters (about 4 per token);
context sizes per call are exact, from the usage Claude Code records.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field

from .model import Call, Session, Step

CHARS_PER_TOKEN = 4


def est_tokens(chars: int) -> int:
    return chars // CHARS_PER_TOKEN


@dataclass
class Jump:
    before: Call
    after: Call
    delta: int
    cause: Step | None


@dataclass
class TurnStats:
    turn: int
    prompt: str
    calls: int
    peak: int
    output: int
    processed: int  # input tokens processed across calls (what you pay for)
    cache_read: int


@dataclass
class ContextReport:
    calls: list[Call]
    peak: int
    peak_call: Call | None
    output: int
    processed: int
    cache_ratio: float
    compactions: list[Step]
    hogs: list[tuple[str, int, int]] = field(default_factory=list)  # label, chars, count
    rereads: list[tuple[str, int, int]] = field(default_factory=list)  # path, reads, chars
    jumps: list[Jump] = field(default_factory=list)
    turns: list[TurnStats] = field(default_factory=list)
    subagent_calls: int = 0


def _hog_label(s: Step) -> str:
    if s.kind == "read" and s.path:
        return f"Read {os.path.basename(s.path)}"
    if s.kind == "shell":
        cmd = s.text.splitlines()[0] if s.text else ""
        return f"{s.tool}: {cmd[:60]}"
    return f"{s.tool}: {s.text[:60]}" if s.text else (s.tool or s.kind)


def analyse(session: Session) -> ContextReport:
    main = [c for c in session.calls if c.agent == "main"]
    peak_call = max(main, key=lambda c: c.usage.context, default=None)
    processed = sum(c.usage.context for c in main)
    cache_read = sum(c.usage.cache_read for c in main)

    hogs: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    reads: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for s in session.steps:
        if s.agent != "main" or s.kind in ("prompt", "compact") or not s.output_chars:
            continue
        h = hogs[_hog_label(s)]
        h[0] += s.output_chars
        h[1] += 1
        if s.kind == "read" and s.path:
            r = reads[s.path]
            r[0] += 1
            r[1] += s.output_chars

    jumps = []
    by_index = session.steps
    for a, b in zip(main, main[1:]):
        delta = b.usage.context - a.usage.context
        if delta <= 0:
            continue
        between = [s for s in by_index[a.step_index + 1 : b.step_index + 1] if s.agent == "main" and s.output_chars]
        cause = max(between, key=lambda s: s.output_chars, default=None)
        jumps.append(Jump(a, b, delta, cause))
    jumps.sort(key=lambda j: j.delta, reverse=True)

    turns: list[TurnStats] = []
    grouped: dict[int, list[Call]] = defaultdict(list)
    for c in main:
        grouped[c.turn].append(c)
    for t in sorted(grouped):
        cs = grouped[t]
        p = session.prompt_for_turn(t)
        turns.append(TurnStats(
            turn=t, prompt=p.text if p else "(before first prompt)", calls=len(cs),
            peak=max(c.usage.context for c in cs), output=sum(c.usage.output for c in cs),
            processed=sum(c.usage.context for c in cs), cache_read=sum(c.usage.cache_read for c in cs),
        ))

    return ContextReport(
        calls=main,
        peak=peak_call.usage.context if peak_call else 0,
        peak_call=peak_call,
        output=sum(c.usage.output for c in main),
        processed=processed,
        cache_ratio=cache_read / processed if processed else 0.0,
        compactions=[s for s in session.steps if s.kind == "compact"],
        hogs=sorted(((k, v[0], v[1]) for k, v in hogs.items()), key=lambda x: x[1], reverse=True)[:12],
        rereads=sorted(((k, v[0], v[1]) for k, v in reads.items() if v[0] > 1), key=lambda x: x[2], reverse=True)[:10],
        jumps=jumps[:8],
        turns=turns,
        subagent_calls=len(session.calls) - len(main),
    )
