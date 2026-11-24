"""Plain data types shared by every part of rewind."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Usage:
    """Token usage of one model call, as Claude Code records it."""

    input: int = 0
    cache_read: int = 0
    cache_write: int = 0
    output: int = 0

    @property
    def context(self) -> int:
        """Size of the context window the model saw on this call."""
        return self.input + self.cache_read + self.cache_write

    @property
    def cache_ratio(self) -> float:
        return self.cache_read / self.context if self.context else 0.0


@dataclass
class Call:
    """One model API call (one assistant message)."""

    timestamp: str
    agent: str
    turn: int
    usage: Usage
    step_index: int  # index of the last step that existed when the call was made


@dataclass
class Step:
    """One thing that happened in a session, in order.

    kind is one of: prompt, edit, write, shell, read, tool, compact.
    """

    index: int
    kind: str
    timestamp: str
    turn: int
    agent: str = "main"
    tool: str | None = None
    tool_id: str | None = None
    path: str | None = None
    text: str = ""
    why: str = ""
    input: dict = field(default_factory=dict)
    result: dict | None = None
    output: str = ""
    output_chars: int = 0
    error: bool = False
    done_timestamp: str | None = None
    context: int = 0  # context size of the call that issued this step

    @property
    def number(self) -> int:
        """1-based number shown to people."""
        return self.index + 1

    @property
    def effective_timestamp(self) -> str:
        return self.done_timestamp or self.timestamp

    @property
    def is_mutation_tool(self) -> bool:
        return self.kind in ("edit", "write")


@dataclass
class Observation:
    """The exact content of a file at a moment, seen somewhere in the record.

    content None means the file did not exist at that moment.
    """

    path: str
    timestamp: str
    content: str | None
    source: str  # backup | read


@dataclass
class Session:
    id: str
    file: str
    title: str
    cwd: str
    branch: str | None
    steps: list[Step]
    calls: list[Call]
    observations: list[Observation]
    started: str
    ended: str
    agents: dict[str, str] = field(default_factory=dict)  # agent id -> agent type

    def prompt_for_turn(self, turn: int) -> Step | None:
        for s in self.steps:
            if s.kind == "prompt" and s.turn == turn:
                return s
        return None

    @property
    def prompts(self) -> list[Step]:
        return [s for s in self.steps if s.kind == "prompt"]
