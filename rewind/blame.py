"""Attribute each line of a file to the agent step (and prompt) that last wrote it.

Every session in the project that edited the file is replayed in time order.
Lines that changed between sessions, or since the last session, without an
agent edit explaining it are attributed to "outside" (you, a formatter, a
merge, a branch switch).
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass
from pathlib import Path

from .reconstruct import Mutation, Timeline, build_timeline, file_key, known, norm
from .transcript import load_session, session_files


@dataclass(frozen=True)
class Source:
    kind: str  # agent | outside | origin
    session_id: str = ""
    session_title: str = ""
    step_number: int = 0
    turn: int = 0
    timestamp: str = ""
    prompt: str = ""
    why: str = ""
    tool: str = ""

    @property
    def label(self) -> str:
        if self.kind == "agent":
            return f"{self.session_id[:6]} T{self.turn} #{self.step_number}"
        if self.kind == "outside":
            return "other (shell/you)"
        return "before agents"


@dataclass
class BlameLine:
    number: int
    text: str
    source: Source


def _mentions(path: Path, needle: str) -> bool:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return False
    return needle.encode("utf-8", errors="ignore").lower() in data.lower()


def timelines_touching(target: str, project_dir: Path, home: Path, limit: int | None = None) -> list[Timeline]:
    key = file_key(target)
    name = os.path.basename(target)
    out = []
    files = session_files(project_dir)
    if limit:
        files = files[:limit]
    for f in files:
        if not _mentions(f, name):
            continue
        s = load_session(f, home)
        tl = build_timeline(s, only={key})
        if key in tl.files:
            out.append(tl)
    out.sort(key=lambda t: t.session.started)
    return out


def _source(tl: Timeline, m: Mutation) -> Source:
    p = tl.session.prompt_for_turn(m.step.turn)
    return Source(
        kind="agent", session_id=tl.session.id, session_title=tl.session.title, step_number=m.step.number,
        turn=m.step.turn, timestamp=m.step.effective_timestamp, prompt=p.text if p else "", why=m.step.why,
        tool=m.step.tool or "",
    )


def _carry(old_lines: list[str], old_src: list[Source], new_lines: list[str], src: Source) -> list[Source]:
    out: list[Source] = []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.extend(old_src[i1:i2])
        else:
            out.extend([src] * (j2 - j1))
    return out


def blame(target: str, timelines: list[Timeline]) -> list[BlameLine]:
    key = file_key(target)
    versions: list[tuple[str, int, object, Source | None]] = []
    for tl in timelines:
        fh = tl.files[key]
        for m in fh.mutations:
            # The state just before each edit: anything that differs from the previous
            # version got there some other way (shell, formatter, you).
            versions.append((m.step.effective_timestamp, 0, m.before, None))
            versions.append((m.step.effective_timestamp, 1, m.after, _source(tl, m)))
    versions.sort(key=lambda v: (v[0], v[1]))

    origin = Source(kind="origin")
    outside = Source(kind="outside")
    lines: list[str] | None = None
    srcs: list[Source] = []
    for _ts, _order, content, src in versions:
        if not known(content):
            continue
        new_lines = content.splitlines() if isinstance(content, str) else []
        if lines is None:
            lines = new_lines
            srcs = [src or origin] * len(new_lines)
            continue
        if new_lines == lines:
            continue
        srcs = _carry(lines, srcs, new_lines, src or outside)
        lines = new_lines

    disk_path = Path(target)
    disk = norm(disk_path.read_text(encoding="utf-8", errors="replace")) if disk_path.exists() else ""
    disk_lines = disk.splitlines()
    if lines is None:
        return [BlameLine(i + 1, t, origin) for i, t in enumerate(disk_lines)]
    if disk_lines != lines:
        srcs = _carry(lines, srcs, disk_lines, outside)
    return [BlameLine(i + 1, t, s) for i, (t, s) in enumerate(zip(disk_lines, srcs))]
