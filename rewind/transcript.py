"""Find Claude Code session transcripts and parse them into a Session.

Claude Code writes one JSONL file per session under
``~/.claude/projects/<encoded project path>/<session id>.jsonl``; subagents
get their own files in ``<session id>/subagents/``; checkpoint backups of
edited files live in ``~/.claude/file-history/<session id>/``.
"""

from __future__ import annotations

import heapq
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .model import Call, Observation, Session, Step, Usage

MUTATING_TOOLS = {"Edit": "edit", "MultiEdit": "edit", "Write": "write"}
SHELL_TOOLS = {"Bash", "PowerShell"}
READ_TOOLS = {"Read"}

# Wrappers the IDE and harness put around what the person typed.
_NOISE_TAGS = re.compile(
    r"<(system-reminder|ide_opened_file|ide_selection|ide_diagnostics|"
    r"local-command-stdout|local-command-stderr|command-message|command-args|task-notification)"
    r"[^>]*>.*?</\1>",
    re.S,
)
_COMMAND_NAME = re.compile(r"<command-name>(.*?)</command-name>", re.S)


class SessionNotFound(Exception):
    pass


def claude_home(override: str | None = None) -> Path:
    if override:
        return Path(override)
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env) if env else Path.home() / ".claude"


def project_key(path: str) -> str:
    """Claude Code's folder name for a project: every non-alphanumeric char -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def project_dir_for(project: str, home: Path) -> Path | None:
    key = project_key(os.path.abspath(project)).lower()
    base = home / "projects"
    if not base.is_dir():
        return None
    dirs = [d for d in base.iterdir() if d.is_dir()]
    for d in dirs:
        if d.name.lower() == key:
            return d
    # Claude Code shortens very long folder names (prefix + hash suffix).
    for d in dirs:
        stem = d.name.lower().rsplit("-", 1)[0]
        if len(stem) >= 80 and key.startswith(stem):
            return d
    return None


def session_files(project_dir: Path) -> list[Path]:
    """Session transcripts in a project folder, newest first."""
    files = [p for p in project_dir.glob("*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def resolve_session(ref: str | None, project: str, home: Path) -> Path:
    """Turn 'latest', a list number, an id prefix or a file path into a transcript path."""
    if ref and ref.endswith(".jsonl") and Path(ref).is_file():
        return Path(ref)
    pdir = project_dir_for(project, home)
    if pdir is None:
        raise SessionNotFound(
            f"No Claude Code sessions recorded for {os.path.abspath(project)} "
            f"(looked in {home / 'projects'}). Use -p to point at the project folder."
        )
    files = session_files(pdir)
    if not files:
        raise SessionNotFound(f"No sessions in {pdir}")
    if not ref or ref in ("latest", "last"):
        return files[0]
    if ref.isdigit() and len(ref) <= 3:
        n = int(ref)
        if 1 <= n <= len(files):
            return files[n - 1]
        raise SessionNotFound(f"There are only {len(files)} sessions; #{n} does not exist")
    matches = [f for f in files if f.stem.startswith(ref)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SessionNotFound(f"No session id starts with '{ref}' in {pdir}")
    raise SessionNotFound(f"'{ref}' matches {len(matches)} sessions; give more of the id")


def _read_jsonl(path: Path) -> Iterator[dict]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                yield rec


def _timestamp(rec: dict) -> str | None:
    ts = rec.get("timestamp")
    if ts:
        return ts
    snap = rec.get("snapshot")
    if isinstance(snap, dict):
        return snap.get("timestamp")
    return None


def _with_order(path: Path, agent: str, file_order: int) -> Iterator[tuple[str, int, int, str, dict]]:
    """Yield (timestamp, file order, line, agent, record), carrying timestamps forward."""
    last = ""
    for n, rec in enumerate(_read_jsonl(path)):
        ts = _timestamp(rec) or last
        last = ts
        yield (ts, file_order, n, agent, rec)


def clean_prompt(text: str) -> str:
    cmd = _COMMAND_NAME.search(text)
    text = _NOISE_TAGS.sub("", text)
    text = _COMMAND_NAME.sub("", text)
    text = text.strip()
    if cmd and not text:
        text = cmd.group(1).strip()
    elif cmd:
        text = f"{cmd.group(1).strip()} {text}"
    return text


def _prompt_text(content) -> str:
    if isinstance(content, str):
        return clean_prompt(content)
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return clean_prompt("\n".join(p for p in parts if p))
    return ""


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _summary(name: str, inp: dict) -> str:
    if name in SHELL_TOOLS:
        return str(inp.get("command", "")).strip()
    for k in ("file_path", "notebook_path", "path", "pattern", "query", "url", "description", "prompt"):
        if inp.get(k):
            return str(inp[k]).strip()
    return ""


def _kind(name: str) -> str:
    if name in MUTATING_TOOLS:
        return MUTATING_TOOLS[name]
    if name in SHELL_TOOLS:
        return "shell"
    if name in READ_TOOLS:
        return "read"
    return "tool"


def _int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@dataclass
class _Backup:
    path: str
    name: str | None
    timestamp: str


def load_session(path: Path, home: Path) -> Session:
    path = Path(path)
    session_id = path.stem
    sources = [_with_order(path, "main", 0)]
    agents: dict[str, str] = {}
    sub_dir = path.with_suffix("") / "subagents"
    if sub_dir.is_dir():
        for i, sub in enumerate(sorted(sub_dir.glob("*.jsonl")), start=1):
            agent_id = sub.stem.removeprefix("agent-")
            meta = sub.with_suffix(".meta.json")
            agent_type = "subagent"
            if meta.is_file():
                try:
                    agent_type = json.loads(meta.read_text(encoding="utf-8")).get("agentType") or agent_type
                except (OSError, json.JSONDecodeError):
                    pass
            agents[agent_id] = agent_type
            sources.append(_with_order(sub, agent_id, i))

    steps: list[Step] = []
    calls: list[Call] = []
    backups: list[_Backup] = []
    pending: dict[str, Step] = {}
    why: dict[str, str] = {}
    seen_msg: set[str] = set()
    context_now: dict[str, int] = {}
    title = ""
    cwd = ""
    branch = None
    turn = 0
    first_ts = ""
    last_ts = ""

    def add(step_kwargs) -> Step:
        s = Step(index=len(steps), **step_kwargs)
        steps.append(s)
        return s

    for ts, _order, _line, agent, rec in heapq.merge(*sources, key=lambda t: (t[0], t[1], t[2])):
        rtype = rec.get("type")
        if ts:
            first_ts = first_ts or ts
            last_ts = max(last_ts, ts)
        if rtype == "ai-title" and agent == "main":
            title = rec.get("aiTitle") or title
        elif rtype == "file-history-snapshot":
            snap = rec.get("snapshot") or {}
            for p, b in (snap.get("trackedFileBackups") or {}).items():
                if isinstance(b, dict):
                    backups.append(_Backup(p, b.get("backupFileName"), b.get("backupTime") or ts))
        elif rtype == "file-history-delta":
            b = rec.get("backup") or {}
            if rec.get("trackingPath"):
                backups.append(_Backup(rec["trackingPath"], b.get("backupFileName"), b.get("backupTime") or ts))
        elif rtype == "system" and rec.get("subtype") == "compact_boundary" and agent == "main":
            meta = rec.get("compactMetadata") or {}
            pre, post = meta.get("preTokens"), meta.get("postTokens")
            text = "Context compacted"
            if pre and post:
                text = f"Context compacted {pre:,} → {post:,} tokens ({meta.get('trigger', 'auto')})"
            add(dict(kind="compact", timestamp=ts, turn=turn, agent=agent, text=text))
        elif rtype == "user":
            if agent == "main":
                cwd = cwd or rec.get("cwd") or ""
                branch = rec.get("gitBranch") or branch
            msg = rec.get("message") or {}
            content = msg.get("content")
            results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"] if isinstance(content, list) else []
            if results:
                tur = rec.get("toolUseResult")
                for block in results:
                    step = pending.pop(block.get("tool_use_id", ""), None)
                    if step is None:
                        continue
                    text = _result_text(block)
                    step.output = text[-4000:]
                    step.output_chars = len(text)
                    step.error = bool(block.get("is_error"))
                    step.done_timestamp = ts
                    if isinstance(tur, dict) and len(results) == 1:
                        step.result = tur
                continue
            if agent != "main" or rec.get("isMeta") or rec.get("isCompactSummary") or rec.get("isSidechain"):
                continue
            text = _prompt_text(content)
            if not text or text.startswith("[Request interrupted"):
                continue
            turn += 1
            why.clear()
            add(dict(kind="prompt", timestamp=ts, turn=turn, agent=agent, text=text))
        elif rtype == "assistant":
            msg = rec.get("message") or {}
            usage = msg.get("usage")
            mid = msg.get("id") or rec.get("uuid") or ""
            if isinstance(usage, dict) and mid and mid not in seen_msg:
                seen_msg.add(mid)
                u = Usage(
                    input=_int(usage.get("input_tokens")),
                    cache_read=_int(usage.get("cache_read_input_tokens")),
                    cache_write=_int(usage.get("cache_creation_input_tokens")),
                    output=_int(usage.get("output_tokens")),
                )
                if u.context:
                    calls.append(Call(ts, agent, turn, u, len(steps) - 1))
                    context_now[agent] = u.context
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    why[agent] = block["text"].strip()
                elif btype == "tool_use":
                    name = block.get("name", "")
                    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                    step = add(
                        dict(
                            kind=_kind(name),
                            timestamp=ts,
                            turn=turn,
                            agent=agent,
                            tool=name,
                            tool_id=block.get("id"),
                            path=inp.get("file_path") or inp.get("notebook_path"),
                            text=_summary(name, inp),
                            why=why.get(agent, ""),
                            input=inp,
                            context=context_now.get(agent, 0),
                        )
                    )
                    if block.get("id"):
                        pending[block["id"]] = step

    observations = _observations(steps, backups, home / "file-history" / session_id)
    return Session(
        id=session_id,
        file=str(path),
        title=title or (steps[0].text[:60] if steps and steps[0].kind == "prompt" else session_id[:8]),
        cwd=cwd,
        branch=branch,
        steps=steps,
        calls=calls,
        observations=observations,
        started=first_ts,
        ended=last_ts,
        agents=agents,
    )


def _observations(steps: list[Step], backups: list[_Backup], backup_dir: Path) -> list[Observation]:
    """Exact file contents seen during the session, for files the session edited."""
    edited = {os.path.normcase(os.path.normpath(s.path)) for s in steps if s.is_mutation_tool and s.path}
    out: list[Observation] = []
    seen: set[tuple[str, str | None]] = set()
    for b in backups:
        key = os.path.normcase(os.path.normpath(b.path))
        if key not in edited or (key, b.name) in seen:
            continue
        seen.add((key, b.name))
        if b.name is None:
            out.append(Observation(b.path, b.timestamp, None, "backup"))
            continue
        f = backup_dir / b.name
        try:
            out.append(Observation(b.path, b.timestamp, f.read_text(encoding="utf-8", errors="replace"), "backup"))
        except OSError:
            continue
    for s in steps:
        if s.kind != "read" or s.error or not isinstance(s.result, dict) or not s.path:
            continue
        key = os.path.normcase(os.path.normpath(s.path))
        f = s.result.get("file")
        if key not in edited or not isinstance(f, dict) or not isinstance(f.get("content"), str):
            continue
        total = _int(f.get("totalLines"), -1)
        if _int(f.get("startLine"), 1) == 1 and total >= 0 and _int(f.get("numLines"), -2) >= total:
            out.append(Observation(s.path, s.effective_timestamp, f["content"], "read"))
    return out


@dataclass
class SessionSummary:
    file: Path
    id: str
    title: str
    started: str
    ended: str
    prompts: int
    edits: int
    files: int
    first_prompt: str


def summarize(path: Path) -> SessionSummary:
    """A quick pass over a transcript for listings (no reconstruction)."""
    title = ""
    started = ended = ""
    prompts = edits = 0
    files: set[str] = set()
    first_prompt = ""
    for rec in _read_jsonl(path):
        ts = _timestamp(rec)
        if ts:
            started = started or ts
            ended = max(ended, ts)
        t = rec.get("type")
        if t == "ai-title":
            title = rec.get("aiTitle") or title
        elif t == "user" and not rec.get("isMeta") and not rec.get("isCompactSummary"):
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                continue
            text = _prompt_text(content)
            if text and not text.startswith("[Request interrupted"):
                prompts += 1
                first_prompt = first_prompt or text
        elif t == "assistant":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") in MUTATING_TOOLS:
                        edits += 1
                        fp = (b.get("input") or {}).get("file_path")
                        if fp:
                            files.add(os.path.normcase(fp))
    return SessionSummary(path, path.stem, title, started, ended, prompts, edits, len(files), first_prompt)
