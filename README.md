# rewind

**`git bisect` for AI agent sessions.** When an agent session breaks your
tests somewhere in its 200 edits, rewind finds the exact edit. It also shows
the prompt that led to it and what the agent said it was doing.

It reads the transcripts Claude Code already writes. There are no hooks to
install and nothing to set up in advance, so it works on sessions you ran
last week.

```
rewind bisect -- pytest -q tests/test_pricing.py
```

```
  run  1  ░▒▒▒▒▒▒▒▒▒▒▒▒▒█  ✗ bad   state 14/14  after #24 README.md
  run  2  █▒▒▒▒▒▒▒▒▒▒▒▒▒█  ✓ good  state  0/14  before the session
  run  3  █▒▒▒▒▒▒█▒▒▒▒▒▒█  ✗ bad   state  7/14  after #11 shop/pricing.py
  run  4  █▒▒█▒▒▒█░░░░░░█  ✓ good  state  3/14  after #7 shop/money.py
  run  5  █░░█▒█▒█░░░░░░█  ✓ good  state  5/14  after #9 shop/pricing.py
  run  6  █░░█░███░░░░░░█  ✗ bad   state  6/14  after #10 shop/pricing.py
  …
┌─ first bad edit ───────────────────────────────────────────────────
│ #10 Edit shop/pricing.py   +3 −1   turn 2 · 2026-11-20 15:32
│ turn 2, you asked:
│   Totals drift by fractions of a cent on big baskets. Move the money maths to integer cents.
│ the agent said:
│   Now the discount: compute the amount off in cents.
│ -    return round(amount * (100 - pct) / 100, 2)
│ +    cents = to_cents(amount)
│ +    off = cents * pct // 100
│ +    return from_cents(cents - off)
│ check output right after this edit:
│   apply_discount(19.99, 'SAVE15') = 17.00   expected 16.99   <-- WRONG
```

To try it without any sessions of your own, run `rewind demo`.

## How it works

**1. Rebuild every step.** Claude Code's transcript records each edit's old
and new text and a line-numbered patch. Some edits also record the file's
full original content. rewind replays the edits forward from every exact
source it can find: those original contents, full-file Reads, checkpoint
backups and, for the gaps, git history. It also undoes the edits backward
from the file on disk today. A rebuilt state is accepted only if the edit
applies to it cleanly, and an undo counts only if redoing the edit gives back
the same text. Across 40 real sessions with about 3,000 file states, 98.7%
were rebuilt exactly. `rewind files` labels every state: exact, inferred, or
unknown.

**2. Notice when something else changed the file.** If the file on disk does
not match what the agent last wrote, something outside its edit tools changed
it: a `sed -i`, a formatter, a branch switch, or you. rewind reports which
step that happened at and which shell commands probably did it.

**3. Bisect.** rewind copies the project into a throwaway folder; your working
tree is never touched. It sets every edited file to its state at step *j*
and runs your check. Exit codes follow `git bisect`: 0 means good, 125 means
skip, anything else means bad. It searches with O(log n) runs, then re-runs
the two states either side of the culprit to rule out a flaky check. It
writes a markdown report with a ready-to-paste prompt for your agent to fix
the problem.

## Commands

| | |
|---|---|
| `rewind sessions` | sessions recorded for this project |
| `rewind tui [S]` | interactive explorer: timeline, prompts, the agent's reasoning, diffs, context use. Keys: `e` edits only, `n`/`p` next/previous prompt, `x` export the state at a step |
| `rewind show [S]` | the timeline in the terminal |
| `rewind step [S] N` | one step in detail, with its diff |
| `rewind files [S]` | files the session edited, reconstruction quality per state, changes made outside the agent's edits |
| `rewind bisect [S] -- <check>` | find the edit that broke the check |
| `rewind checkout [S] --step N --out DIR [--full]` | the code as it was right after step N |
| `rewind blame FILE [-L a,b]` | which session, turn and prompt wrote each line, across all sessions |
| `rewind context [S]` | where the context window went: per-turn peaks, cache hit rate, biggest tool outputs, files read repeatedly, largest jumps, compactions |

`S` is `latest` (the default), a number from `rewind sessions`, an id prefix,
or a path to a `.jsonl` file. Use `-p PATH` for sessions run in another
project folder.

Useful `bisect` options:

- `--hold tests/` keeps paths at today's version in every state. Use it when
  the session also edited the test you are bisecting with.
- `--good-step` / `--bad-step` narrow the search.
- `--timeout` sets the time limit per run.
- `--keep` keeps the sandbox folder afterwards.

## Limits

- Shell commands are not replayed. If one changed an edited file, the next
  recorded original content catches it. If one changed a file the agent
  never edited (a migration, a lockfile), the sandbox keeps today's version
  of that file.
- Files the session did not edit are taken as they are on disk today.
- The test environment (virtualenvs, `node_modules`) is not copied. Point the
  check at your real interpreter, e.g. `-- C:/proj/.venv/Scripts/python -m pytest`.
- Claude Code only, for now. The transcript parser is the only part that
  depends on Claude Code.

## Install

```
pip install -e .
rewind demo
```

Requires Python 3.10+, `rich` and `textual`.

---

Engineered by **Krish Desai**.
