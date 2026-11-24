"""Binary search over an agent session's edits for the one that broke a check.

State j is the project after the session's first j edits (j = 0 is before
the session touched anything). The check command follows git bisect's
convention: exit 0 = good, 125 = can't test this state (skip), anything
else = bad.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from typing import Callable

from .reconstruct import Mutation, Timeline
from .sandbox import Sandbox

SKIP_CODE = 125


@dataclass
class Probe:
    j: int
    verdict: str  # good | bad | skip | timeout
    code: int | None
    seconds: float
    output: str
    weak: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class BisectResult:
    status: str  # found | not-reproduced | bad-from-start | ambiguous | flaky
    good: int | None = None
    bad: int | None = None
    culprit: Mutation | None = None
    probes: list[Probe] = field(default_factory=list)
    message: str = ""
    skipped_range: list[Mutation] = field(default_factory=list)

    @property
    def runs(self) -> int:
        return len(self.probes)


def run_check(sandbox: Sandbox, j: int, cmd: str, cwd, timeout: float | None, env_extra: dict) -> Probe:
    import os

    weak = sandbox.apply(j)
    env = dict(os.environ)
    env.update(env_extra)
    env["REWIND_STATE"] = str(j)
    start = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, timeout=timeout, env=env,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or "") + (exc.stderr or "") if isinstance(exc.stdout, str) else ""
        return Probe(j, "timeout", None, time.perf_counter() - start, out[-3000:], weak)
    secs = time.perf_counter() - start
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        verdict = "good"
    elif proc.returncode == SKIP_CODE:
        verdict = "skip"
    else:
        verdict = "bad"
    return Probe(j, verdict, proc.returncode, secs, out[-3000:], weak)


def bisect(
    timeline: Timeline,
    sandbox: Sandbox,
    cmd: str,
    cwd,
    good: int = 0,
    bad: int | None = None,
    timeout: float | None = None,
    confirm: bool = True,
    timeout_is: str = "bad",
    on_probe: Callable[[Probe, int, int], None] | None = None,
) -> BisectResult:
    n = len(timeline.mutations)
    bad = n if bad is None else bad
    result = BisectResult(status="found")
    verdicts: dict[int, str] = {}

    def test(j: int, lo: int, hi: int) -> str:
        p = run_check(sandbox, j, cmd, cwd, timeout, {})
        if p.verdict == "timeout":
            p.verdict = timeout_is
        result.probes.append(p)
        verdicts[j] = p.verdict
        if on_probe:
            on_probe(p, lo, hi)
        return p.verdict

    if test(bad, good, bad) != "bad":
        result.status = "not-reproduced"
        result.bad = bad
        result.message = (
            "The check passes at the end of the session, so there is nothing to bisect. "
            "(If it fails in your working tree, the break came after the session.)"
        )
        return result
    v0 = test(good, good, bad)
    if v0 == "bad":
        result.status = "bad-from-start"
        result.good, result.bad = None, good
        result.message = "The check already fails before the session's first edit: the session did not introduce it."
        return result
    if v0 == "skip":
        result.status = "ambiguous"
        result.message = "The starting state can't be tested (exit 125); pick a later --good state."
        return result

    lo, hi = good, bad
    while hi - lo > 1:
        cands = [j for j in range(lo + 1, hi) if verdicts.get(j) != "skip"]
        if not cands:
            result.status = "ambiguous"
            result.good, result.bad = lo, hi
            result.skipped_range = timeline.mutations[lo:hi]
            result.message = f"Every state between {lo} and {hi} was skipped; the culprit is one of these edits."
            return result
        mid = (lo + hi) / 2
        j = min(cands, key=lambda x: (abs(x - mid), x))
        v = test(j, lo, hi)
        if v == "good":
            lo = j
        elif v == "bad":
            hi = j

    result.good, result.bad = lo, hi
    result.culprit = timeline.mutations[hi - 1]

    if confirm:
        # Re-run the boundary once: a flaky check would otherwise convict an innocent edit.
        vb = test(hi, lo, hi)
        vg = test(lo, lo, hi)
        if vb != "bad" or vg != "good":
            result.status = "flaky"
            result.message = (
                f"The check gave a different answer on a re-run (state {lo}: {vg}, state {hi}: {vb}). "
                "It is probably flaky; the suspect below is unconfirmed."
            )
    return result
