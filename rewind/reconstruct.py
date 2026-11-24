"""Rebuild the content of every edited file at every step of a session.

Sources of truth, best first:

* ``originalFile`` / ``content`` on Edit and Write results (exact),
* Claude Code's checkpoint backups and full-file Reads (exact observations),
* the edit itself (old/new strings, or the line-numbered patch) applied to a
  known state (exact if the state was exact),
* the file on disk today, with the session's edits undone one by one
  ("inferred": right unless something else changed the same lines later).

When two exact sources disagree, the file was changed outside the agent's
edit tools (a shell command, a formatter, or you); rewind records where.
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path

from .model import Observation, Session, Step


class _Unknown:
    def __repr__(self) -> str:
        return "UNKNOWN"

    def __bool__(self) -> bool:
        return False


UNKNOWN = _Unknown()
ABSENT = None  # the file does not exist

QUALITY_RANK = {"exact": 3, "derived": 2, "inferred": 1, "unknown": 0}


def norm(text):
    if isinstance(text, str):
        return text.replace("\r\n", "\n")
    return text


def file_key(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def known(state) -> bool:
    return state is not UNKNOWN


# ---------------------------------------------------------------- patching


def _find_block(lines: list[str], block: list[str], expected: int) -> int | None:
    if not block:
        return max(0, min(expected, len(lines)))
    n, m = len(lines), len(block)
    for dist in range(0, n + 1):
        for pos in ((expected - dist, expected + dist) if dist else (expected,)):
            if 0 <= pos <= n - m and lines[pos : pos + m] == block:
                return pos
        if expected - dist < 0 and expected + dist > n - m:
            break
    return None


def apply_hunks(text: str, hunks: list[dict], reverse: bool = False) -> str | None:
    """Apply a structuredPatch (or undo it). None if it does not fit."""
    if not isinstance(text, str) or not hunks:
        return None
    lines = text.split("\n")
    shift = 0
    for h in hunks:
        old, new = [], []
        for ln in h.get("lines") or []:
            if not ln or ln.startswith("\\"):
                continue
            tag, body = ln[0], ln[1:].rstrip("\r")
            if tag in " -":
                old.append(body)
            if tag in " +":
                new.append(body)
        src, dst = (new, old) if reverse else (old, new)
        start = int(h.get("newStart" if reverse else "oldStart") or 1)
        start = max(start - 1, 0) if src else start
        expected = start + shift
        pos = _find_block(lines, src, expected)
        if pos is None:
            return None
        lines[pos : pos + len(src)] = dst
        shift += (pos - expected) + len(dst) - len(src)
    return "\n".join(lines)


def _edits_of(step: Step) -> list[tuple[str, str, bool]]:
    r = step.result if isinstance(step.result, dict) else {}
    if step.tool == "MultiEdit":
        edits = r.get("edits") or step.input.get("edits") or []
        return [
            (norm(e.get("old_string", e.get("oldString", ""))), norm(e.get("new_string", e.get("newString", ""))),
             bool(e.get("replace_all", e.get("replaceAll", False))))
            for e in edits
            if isinstance(e, dict)
        ]
    old = r.get("oldString", step.input.get("old_string", ""))
    new = r.get("newString", step.input.get("new_string", ""))
    ra = r.get("replaceAll", step.input.get("replace_all", False))
    return [(norm(old or ""), norm(new or ""), bool(ra))]


def forward(step: Step, before):
    """Content after the step, given content before it. UNKNOWN if it can't be done."""
    r = step.result if isinstance(step.result, dict) else {}
    if step.kind == "write":
        content = r.get("content", step.input.get("content"))
        return norm(content) if isinstance(content, str) else UNKNOWN
    if not known(before):
        return UNKNOWN
    text = before
    ok = True
    for old, new, replace_all in _edits_of(step):
        if old == "" and (text is None or text == ""):
            text = new
        elif isinstance(text, str) and old and old in text:
            text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        else:
            ok = False
            break
    if ok and isinstance(text, str):
        return text
    patched = apply_hunks(before or "", r.get("structuredPatch") or [])
    return patched if patched is not None else UNKNOWN


def backward(step: Step, after):
    """Content before the step, given content after it. UNKNOWN if it can't be done."""
    r = step.result if isinstance(step.result, dict) else {}
    if step.kind == "write":
        if r.get("type") == "create":
            return ABSENT
        orig = r.get("originalFile")
        return norm(orig) if isinstance(orig, str) else UNKNOWN
    if not isinstance(after, str):
        return UNKNOWN
    candidates = []
    patched = apply_hunks(after, r.get("structuredPatch") or [], reverse=True)
    if patched is not None:
        candidates.append(patched)
    edits = _edits_of(step)
    text = after
    for old, new, replace_all in reversed(edits):
        if new and new in text:
            text = text.replace(new, old) if replace_all else text.replace(new, old, 1)
        else:
            text = None
            break
    if text is not None:
        candidates.append(text)
    for c in candidates:
        if forward(step, c) == after:
            return c
    return UNKNOWN


# ---------------------------------------------------------------- timeline


@dataclass
class Mutation:
    seq: int
    step: Step
    key: str
    before: object = UNKNOWN
    after: object = UNKNOWN
    before_q: str = "unknown"
    after_q: str = "unknown"

    @property
    def path(self) -> str:
        return self.step.path or ""

    def diff(self, context: int = 3) -> str:
        a = self.before if isinstance(self.before, str) else ""
        b = self.after if isinstance(self.after, str) else ""
        if not known(self.before) or not known(self.after):
            return "\n".join(_patch_lines(self.step))
        name = os.path.basename(self.path)
        return "".join(
            difflib.unified_diff(
                a.splitlines(keepends=True), b.splitlines(keepends=True),
                fromfile=f"a/{name}", tofile=f"b/{name}", n=context,
            )
        )

    @property
    def changed_lines(self) -> tuple[int, int]:
        a = self.before.splitlines() if isinstance(self.before, str) else []
        b = self.after.splitlines() if isinstance(self.after, str) else []
        added = removed = 0
        for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
            if op != "equal":
                removed += i2 - i1
                added += j2 - j1
        return added, removed


def _patch_lines(step: Step) -> list[str]:
    r = step.result if isinstance(step.result, dict) else {}
    out = []
    for h in r.get("structuredPatch") or []:
        out.append(f"@@ -{h.get('oldStart')},{h.get('oldLines')} +{h.get('newStart')},{h.get('newLines')} @@")
        out.extend(h.get("lines") or [])
    return out


@dataclass
class ExternalChange:
    """The file changed between two points without an agent edit explaining it."""

    key: str
    path: str
    after_seq: int | None  # last mutation seq before the change (None = before first)
    timestamp: str
    detail: str
    suspects: list[Step] = field(default_factory=list)


@dataclass
class FileHistory:
    key: str
    path: str
    mutations: list[Mutation]
    eol: str = "\n"
    disk: object = UNKNOWN
    external: list[ExternalChange] = field(default_factory=list)

    @property
    def final(self):
        return self.mutations[-1].after if self.mutations else UNKNOWN

    @property
    def matches_disk(self) -> bool | None:
        if not known(self.final) or not known(self.disk):
            return None
        return self.final == self.disk

    @property
    def baseline(self):
        return self.mutations[0].before if self.mutations else UNKNOWN

    def fidelity(self) -> float:
        states = [self.mutations[0].before_q] + [m.after_q for m in self.mutations]
        good = sum(1 for q in states if q in ("exact", "derived"))
        return good / len(states)


@dataclass
class Timeline:
    session: Session
    mutations: list[Mutation]
    files: dict[str, FileHistory]

    def state_at(self, j: int) -> dict[str, tuple[object, str]]:
        """Content of every edited file after the first j mutations: key -> (content, quality)."""
        return {key: self.file_state(key, j) for key in self.files}

    def file_state(self, key: str, j: int) -> tuple[object, str]:
        """Content of one edited file after the first j mutations: (content, quality)."""
        fh = self.files[key]
        idx = None
        for i, m in enumerate(fh.mutations):
            if m.seq < j:
                idx = i
            else:
                break
        if idx is None:
            content, q = fh.mutations[0].before, fh.mutations[0].before_q
            pos = -1
        else:
            content, q = fh.mutations[idx].after, fh.mutations[idx].after_q
            pos = idx
        if not known(content):
            content, q = _nearest_known(fh, pos)
        return content, q

    def mutations_through_step(self, step_index: int) -> int:
        """How many mutations happened at or before a step (step index is 0-based)."""
        return sum(1 for m in self.mutations if m.step.index <= step_index)

    def fidelity(self) -> tuple[int, int]:
        total = good = 0
        for fh in self.files.values():
            states = [fh.mutations[0].before_q] + [m.after_q for m in fh.mutations]
            total += len(states)
            good += sum(1 for q in states if q in ("exact", "derived"))
        return good, total

    @property
    def external_changes(self) -> list[ExternalChange]:
        return sorted((e for fh in self.files.values() for e in fh.external), key=lambda e: e.timestamp)


def _nearest_known(fh: FileHistory, pos: int):
    states = [(fh.mutations[0].before, fh.mutations[0].before_q)] + [(m.after, m.after_q) for m in fh.mutations]
    i = pos + 1
    for d in range(1, len(states) + 1):
        for k in (i - d, i + d):
            if 0 <= k < len(states) and known(states[k][0]):
                return states[k][0], "approx"
    if known(fh.disk):
        return fh.disk, "approx"
    return UNKNOWN, "unknown"


class GitBlobs:
    """Versions of a file from git history, nearest in time first, fetched lazily."""

    def __init__(self, max_versions: int = 40):
        self.max_versions = max_versions
        self._log: dict[str, list[tuple[float, str, str, str]]] = {}
        self._blob: dict[tuple[str, str], str | None] = {}

    @staticmethod
    def _git(args: list[str], cwd: str) -> str | None:
        import subprocess

        try:
            out = subprocess.run(
                ["git", *args], cwd=cwd, capture_output=True, timeout=20,
                encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return out.stdout if out.returncode == 0 else None

    def _versions(self, path: str) -> list[tuple[float, str, str, str]]:
        key = file_key(path)
        if key in self._log:
            return self._log[key]
        from datetime import datetime

        versions: list[tuple[float, str, str, str]] = []
        d = os.path.dirname(path)
        while d and not os.path.isdir(d):
            d = os.path.dirname(d)
        root = self._git(["rev-parse", "--show-toplevel"], d) if d else None
        if root:
            root = root.strip()
            rel = os.path.relpath(path, root).replace("\\", "/")
            log = self._git(["log", "--all", f"-n{self.max_versions}", "--format=%H%x09%cI", "--", rel], root) or ""
            for line in log.splitlines():
                sha, _, when = line.partition("\t")
                try:
                    t = datetime.fromisoformat(when.strip()).timestamp()
                except ValueError:
                    continue
                versions.append((t, sha, root, rel))
        self._log[key] = versions
        return versions

    def candidates(self, path: str, timestamp: str):
        from datetime import datetime

        try:
            t0 = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            t0 = 0.0
        for t, sha, root, rel in sorted(self._versions(path), key=lambda v: abs(v[0] - t0)):
            k = (sha, rel)
            if k not in self._blob:
                self._blob[k] = self._git(["show", f"{sha}:{rel}"], root)
            if self._blob[k] is not None:
                yield norm(self._blob[k])


def _read_disk(path: str):
    p = Path(path)
    if not p.exists():
        return ABSENT, "\n"
    try:
        raw = p.read_bytes()
    except OSError:
        return UNKNOWN, "\n"
    text = raw.decode("utf-8", errors="replace")
    eol = "\r\n" if "\r\n" in text else "\n"
    return norm(text), eol


def build_timeline(session: Session, read_disk: bool = True, use_git: bool = True,
                   only: set[str] | None = None, progress=None) -> Timeline:
    """Reconstruct the session.

    only: limit to these file keys (see file_key).
    progress: called as progress(done, total, path) as each file is rebuilt.
    """
    git = GitBlobs() if use_git else None
    mutations: list[Mutation] = []
    for step in session.steps:
        if not step.is_mutation_tool or step.error or not step.path or step.done_timestamp is None:
            continue
        if only is not None and file_key(step.path) not in only:
            continue
        if step.result is None and step.kind == "edit":
            # An edit with no structured result: only trust it if the output says it worked.
            if "updated" not in step.output and "success" not in step.output.lower():
                continue
        mutations.append(Mutation(seq=len(mutations), step=step, key=file_key(step.path)))

    obs_by_key: dict[str, list[Observation]] = {}
    for o in session.observations:
        obs_by_key.setdefault(file_key(o.path), []).append(o)

    files: dict[str, FileHistory] = {}
    for m in mutations:
        files.setdefault(m.key, FileHistory(m.key, m.path, []))
        files[m.key].mutations.append(m)

    shell_steps = [s for s in session.steps if s.kind == "shell"]

    for n, (key, fh) in enumerate(files.items(), 1):
        if progress is not None:
            progress(n, len(files), fh.path)
        if read_disk:
            fh.disk, fh.eol = _read_disk(fh.path)
        for o in obs_by_key.get(key, []):
            if isinstance(o.content, str) and "\r\n" in o.content:
                fh.eol = "\r\n"
        for m in fh.mutations:
            orig = m.step.result.get("originalFile") if isinstance(m.step.result, dict) else None
            if isinstance(orig, str) and "\r\n" in orig:
                fh.eol = "\r\n"
        _forward_pass(fh, obs_by_key.get(key, []), shell_steps, git)
        if read_disk:
            _backward_pass(fh, shell_steps)

    return Timeline(session, mutations, files)


def _forward_pass(
    fh: FileHistory, observations: list[Observation], shell_steps: list[Step], git: GitBlobs | None = None
) -> None:
    """Replay the edits in order.

    Checkpoint backups are not reliably timestamped (their content can lag
    their backupTime by several edits), so neither they nor Reads are allowed
    to overrule a known state. They only fill gaps, and a candidate is used
    only if the next edit applies to it cleanly.
    """
    candidates = sorted(observations, key=lambda o: o.timestamp)
    reads = [o for o in candidates if o.source == "read"]
    read_i = 0
    state, quality = UNKNOWN, "unknown"
    last_seq: int | None = None
    last_ts = ""

    def conflict(ts: str, detail: str) -> None:
        fh.external.append(
            ExternalChange(fh.key, fh.path, last_seq, ts, detail, _suspects(fh.path, shell_steps, last_ts, ts))
        )

    for m in fh.mutations:
        ts = m.step.effective_timestamp
        # Full-file Reads are timestamped exactly; they fill an unknown state.
        while read_i < len(reads) and reads[read_i].timestamp <= m.step.timestamp:
            if not known(state):
                state, quality = norm(reads[read_i].content), "derived"
            read_i += 1

        r = m.step.result if isinstance(m.step.result, dict) else {}
        orig = r.get("originalFile")
        if isinstance(orig, str):
            orig = norm(orig)
            if known(state) and quality in ("exact", "derived") and orig != state:
                conflict(ts, "file was changed outside the agent's edits before this step")
            state, quality = orig, "exact"
        elif m.step.kind == "write" and r.get("type") == "create":
            if known(state) and state is not ABSENT and quality in ("exact", "derived"):
                conflict(ts, "file was deleted outside the agent's edits before this step")
            state, quality = ABSENT, "exact"

        after = forward(m.step, state)
        if m.step.kind == "edit" and not known(after):
            if known(state) and quality in ("exact", "derived"):
                conflict(ts, "the agent's edit did not apply to its last known version of the file")
            pool = [norm(o.content) for o in reversed([c for c in candidates if c.timestamp <= ts])]
            found = False
            for cand in pool:
                trial = forward(m.step, cand)
                if known(trial):
                    state, quality, after, found = cand, "derived", trial, True
                    break
            if not found and git is not None:
                for cand in git.candidates(fh.path, ts):
                    trial = forward(m.step, cand)
                    if known(trial):
                        state, quality, after, found = cand, "derived", trial, True
                        break
            if not found:
                state, quality = UNKNOWN, "unknown"

        m.before, m.before_q = state, quality
        if known(after):
            m.after = after
            m.after_q = "exact" if m.step.kind == "write" else quality
        else:
            m.after, m.after_q = UNKNOWN, "unknown"
        state, quality = m.after, m.after_q
        last_seq = m.seq
        last_ts = ts


def _backward_pass(fh: FileHistory, shell_steps: list[Step]) -> None:
    """Fill gaps by undoing the session's edits, starting from the file on disk now."""
    state = fh.disk
    q = "inferred"
    for m in reversed(fh.mutations):
        if known(m.after) and m.after_q in ("exact", "derived"):
            state, q = m.before, m.before_q
            if not known(state) and known(m.after):
                b = backward(m.step, m.after)
                if known(b):
                    m.before, m.before_q = b, m.after_q
                    state, q = b, m.after_q
            continue
        if known(state):
            # Only trust the later state as this step's result if undoing the
            # edit and redoing it reproduces it exactly (it really contains the edit).
            b = backward(m.step, state)
            if known(b):
                if not known(m.after):
                    m.after, m.after_q = state, q
                if not known(m.before):
                    m.before, m.before_q = b, "inferred"
            state = m.before
            q = m.before_q if known(m.before) else "unknown"
        else:
            state = m.before
            q = m.before_q
    # Stitch: an unknown 'before' can take the previous mutation's 'after'.
    for prev, cur in zip(fh.mutations, fh.mutations[1:]):
        if not known(cur.before) and known(prev.after):
            cur.before, cur.before_q = prev.after, prev.after_q
        if not known(prev.after) and known(cur.before):
            prev.after, prev.after_q = cur.before, cur.before_q


def _suspects(path: str, shell_steps: list[Step], start: str, end: str) -> list[Step]:
    name = os.path.basename(path).lower()
    window = [s for s in shell_steps if (not start or s.timestamp >= start) and s.timestamp <= end]
    named = [s for s in window if name and name in s.text.lower()]
    return named or window[-3:]
