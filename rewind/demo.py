"""Build a synthetic Claude Code session to try rewind on.

The session is scripted, not recorded: it is written in the same JSONL format
Claude Code uses, and every edit is really applied to a small project so the
files on disk end where the session ends. Somewhere in turn 2 the agent moves
the money maths to integer cents and floors a discount instead of rounding it.
Its own test run passes (it ran the wrong tests); the regression check that
was already in the repo does not.
"""

from __future__ import annotations

import difflib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .transcript import project_key

PRICING_V0 = '''"""Checkout pricing."""

TAX_RATE = 0.08

DISCOUNT_CODES = {
    "SAVE10": 10,
    "SAVE15": 15,
}


def subtotal(items):
    return sum(price * qty for price, qty in items)


def apply_discount(amount, code):
    pct = DISCOUNT_CODES.get(code, 0)
    return round(amount * (100 - pct) / 100, 2)


def total(items, code=None):
    amount = apply_discount(subtotal(items), code)
    return round(amount * (1 + TAX_RATE), 2)
'''

CHECK = '''"""Regression check: discount codes must round to the nearest cent."""
import sys

from shop.pricing import apply_discount

CASES = [
    (19.99, "SAVE15", 16.99),
    (100.00, "SAVE10", 90.00),
    (10.00, "NONE", 10.00),
]

failed = 0
for amount, code, expected in CASES:
    got = apply_discount(amount, code)
    ok = abs(got - expected) < 1e-9
    failed += not ok
    print(f"apply_discount({amount}, {code!r}) = {got:.2f}   expected {expected:.2f}" + ("" if ok else "   <-- WRONG"))
sys.exit(1 if failed else 0)
'''

README_V0 = """# shop

Checkout maths for the shop.

Run `python check_discount.py` before releasing.
"""

TEST_TOTAL = '''from shop.pricing import subtotal, total


def test_subtotal():
    assert subtotal([(2.50, 2), (1.00, 3)]) == 8.00


def test_total_no_code():
    assert total([(10.00, 1)]) == 10.80


def test_total_empty():
    assert total([]) == 0
'''


def _patch(a: str, b: str) -> list[dict]:
    hunks = []
    cur = None
    for line in difflib.unified_diff(a.splitlines(), b.splitlines(), lineterm="", n=3):
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            parts = line.split()
            old, new = parts[1][1:], parts[2][1:]
            os_, ol = (old.split(",") + ["1"])[:2]
            ns, nl = (new.split(",") + ["1"])[:2]
            cur = {"oldStart": int(os_), "oldLines": int(ol), "newStart": int(ns), "newLines": int(nl), "lines": []}
            hunks.append(cur)
        elif cur is not None:
            cur["lines"].append(line)
    return hunks


class _Recorder:
    def __init__(self, home: Path, project: Path, session_id: str):
        self.home = home
        self.project = project
        self.sid = session_id
        self.t = datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc)
        self.records: list[dict] = []
        self.parent = None
        self.context = 18_000
        self.n = 0

    def _ts(self, seconds: float = 9) -> str:
        self.t += timedelta(seconds=seconds)
        return self.t.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _rec(self, rtype: str, seconds: float = 9, **fields) -> dict:
        u = str(uuid.uuid4())
        rec = {
            "parentUuid": self.parent, "isSidechain": False, "type": rtype, "uuid": u,
            "timestamp": self._ts(seconds), "sessionId": self.sid, "cwd": str(self.project),
            "gitBranch": "feature/cents", "version": "demo", **fields,
        }
        self.parent = u
        self.records.append(rec)
        return rec

    def _usage(self, grow: int, out: int) -> dict:
        self.context += grow
        return {"input_tokens": 6, "cache_read_input_tokens": self.context - 1200,
                "cache_creation_input_tokens": 1194, "output_tokens": out}

    def prompt(self, text: str) -> None:
        self._rec("user", 40, message={"role": "user", "content": text})

    def say(self, text: str) -> None:
        self.n += 1
        self._rec("assistant", 6, message={"id": f"msg_demo{self.n}", "role": "assistant", "type": "message",
                                           "content": [{"type": "text", "text": text}],
                                           "usage": self._usage(900, len(text) // 3)})

    def _tool(self, name: str, inp: dict, result_text: str, tur: dict | None, grow: int = 1500) -> None:
        self.n += 1
        tid = f"toolu_demo{self.n:03d}"
        self._rec("assistant", 5, message={"id": f"msg_demo{self.n}", "role": "assistant", "type": "message",
                                           "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}],
                                           "usage": self._usage(grow, 220)})
        extra = {"toolUseResult": tur} if tur is not None else {}
        self._rec("user", 2, message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result_text}]}, **extra)

    def path(self, rel: str) -> Path:
        return self.project / rel

    def edit(self, rel: str, old: str, new: str, with_original: bool = False) -> None:
        p = self.path(rel)
        before = p.read_text(encoding="utf-8")
        assert before.count(old) == 1, f"demo edit is not unique in {rel}: {old!r}"
        after = before.replace(old, new, 1)
        p.write_text(after, encoding="utf-8", newline="\n")
        self._tool("Edit", {"file_path": str(p), "old_string": old, "new_string": new, "replace_all": False},
                   f"The file {p} has been updated successfully.",
                   {"filePath": str(p), "oldString": old, "newString": new,
                    "originalFile": before if with_original else None,
                    "structuredPatch": _patch(before, after), "userModified": False, "replaceAll": False})

    def write(self, rel: str, content: str) -> None:
        p = self.path(rel)
        existed = p.exists()
        before = p.read_text(encoding="utf-8") if existed else None
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8", newline="\n")
        self._tool("Write", {"file_path": str(p), "content": content},
                   f"File created successfully at: {p}" if not existed else f"The file {p} has been updated.",
                   {"type": "update" if existed else "create", "filePath": str(p), "content": content,
                    "structuredPatch": _patch(before or "", content) if existed else [],
                    "originalFile": before})

    def read(self, rel: str) -> None:
        p = self.path(rel)
        content = p.read_text(encoding="utf-8")
        lines = content.count("\n")
        numbered = "\n".join(f"{i + 1:>6}\t{ln}" for i, ln in enumerate(content.splitlines()))
        self._tool("Read", {"file_path": str(p)}, numbered,
                   {"type": "text", "file": {"filePath": str(p), "content": content, "numLines": lines,
                                             "startLine": 1, "totalLines": lines}}, grow=len(content) // 3)

    def bash(self, cmd: str, output: str, effect=None) -> None:
        if effect:
            effect()
        self._tool("Bash", {"command": cmd, "description": cmd}, output,
                   {"stdout": output, "stderr": "", "interrupted": False}, grow=len(output) // 3 + 300)

    def save(self) -> Path:
        pdir = self.home / "projects" / project_key(str(self.project))
        pdir.mkdir(parents=True, exist_ok=True)
        f = pdir / f"{self.sid}.jsonl"
        title = {"type": "ai-title", "aiTitle": "Integer-cents pricing refactor", "sessionId": self.sid}
        with open(f, "w", encoding="utf-8") as fh:
            for r in [*self.records[:1], title, *self.records[1:]]:
                fh.write(json.dumps(r) + "\n")
        return f


def build_demo(base: Path) -> tuple[Path, Path, Path]:
    """Create the demo project and transcript under base. Returns (claude home, project, transcript)."""
    base = Path(base)
    home = base / "claude-home"
    project = base / "shop-app"
    (project / "shop").mkdir(parents=True, exist_ok=True)
    (project / "tests").mkdir(exist_ok=True)
    (project / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (project / "shop" / "pricing.py").write_text(PRICING_V0, encoding="utf-8", newline="\n")
    (project / "check_discount.py").write_text(CHECK, encoding="utf-8", newline="\n")
    (project / "README.md").write_text(README_V0, encoding="utf-8", newline="\n")
    (project / "tests" / "test_total.py").write_text(TEST_TOTAL, encoding="utf-8", newline="\n")

    r = _Recorder(home, project, "5e55a0de-demo-4b1d-9a7e-c0ffee000001")

    # Turn 1
    r.prompt("Add a WELCOME5 discount code, and make codes case-insensitive.")
    r.read("shop/pricing.py")
    r.say("I'll add the code and normalise the lookup to upper case.")
    r.edit("shop/pricing.py", '    "SAVE15": 15,\n', '    "SAVE15": 15,\n    "WELCOME5": 5,\n', with_original=True)
    r.edit("shop/pricing.py", "DISCOUNT_CODES.get(code, 0)", 'DISCOUNT_CODES.get((code or "").upper(), 0)')
    r.bash("python check_discount.py", "apply_discount(19.99, 'SAVE15') = 16.99   expected 16.99\n"
           "apply_discount(100.0, 'SAVE10') = 90.00   expected 90.00\napply_discount(10.0, 'NONE') = 10.00   expected 10.00")
    r.say("Done: WELCOME5 gives 5% off and codes match regardless of case. The regression check passes.")

    # Turn 2 (the bug lands here)
    r.prompt("Totals drift by fractions of a cent on big baskets. Move the money maths to integer cents.")
    r.say("Floats are the cause. I'll add a small money helper and do all arithmetic in integer cents.")
    r.write("shop/money.py", '"""Integer-cent helpers."""\n\n\ndef to_cents(amount):\n    return int(round(amount * 100))\n\n\n'
            'def from_cents(cents):\n    return cents / 100\n')
    r.edit("shop/pricing.py", '"""Checkout pricing."""\n', '"""Checkout pricing."""\n\nfrom .money import from_cents, to_cents\n')
    r.edit("shop/pricing.py", "    return sum(price * qty for price, qty in items)",
           "    return from_cents(sum(to_cents(price) * qty for price, qty in items))")
    r.say("Now the discount: compute the amount off in cents.")
    r.edit("shop/pricing.py",
           "    return round(amount * (100 - pct) / 100, 2)",
           "    cents = to_cents(amount)\n    off = cents * pct // 100\n    return from_cents(cents - off)")
    r.edit("shop/pricing.py",
           "    return round(amount * (1 + TAX_RATE), 2)",
           "    cents = to_cents(amount)\n    return from_cents(cents + round(cents * TAX_RATE))")
    r.bash("python -m pytest -q tests/test_total.py", "...                                                                      [100%]\n3 passed in 0.04s")
    r.say("All money maths now runs in integer cents and the tests pass.")

    # Turn 3
    r.prompt("Add a bulk discount: 5% off any line with quantity 10 or more.")
    r.edit("shop/pricing.py", "TAX_RATE = 0.08\n", "TAX_RATE = 0.08\nBULK_QTY = 10\nBULK_PCT = 5\n")
    r.edit("shop/pricing.py",
           "    return from_cents(sum(to_cents(price) * qty for price, qty in items))",
           "    total_cents = 0\n    for price, qty in items:\n        line = to_cents(price) * qty\n"
           "        if qty >= BULK_QTY:\n            line -= line * BULK_PCT // 100\n        total_cents += line\n"
           "    return from_cents(total_cents)")
    r.write("tests/test_bulk.py", "from shop.pricing import subtotal\n\n\ndef test_bulk_line():\n"
            "    assert subtotal([(1.00, 10)]) == 9.50\n")
    r.bash("python -m pytest -q tests", "....                                                                     [100%]\n4 passed in 0.05s")

    # Turn 4: a formatter run outside the agent's edit tools changes the file
    r.prompt("Tidy up: add docstrings, and run the import sorter.")

    def sort_imports():
        p = project / "shop" / "pricing.py"
        p.write_text(p.read_text(encoding="utf-8").replace(
            '"""Checkout pricing."""\n\nfrom .money import', '"""Checkout pricing."""\n\n# money helpers\nfrom .money import'),
            encoding="utf-8", newline="\n")
    r.bash("python tools/sort_imports.py shop/pricing.py", "1 file reformatted", effect=sort_imports)
    r.edit("shop/pricing.py", "def subtotal(items):\n", 'def subtotal(items):\n    """Sum of price x quantity, with bulk discounts."""\n',
           with_original=True)
    r.edit("shop/pricing.py", "def apply_discount(amount, code):\n",
           'def apply_discount(amount, code):\n    """Apply a discount code to an amount."""\n')
    r.edit("shop/pricing.py", "def total(items, code=None):\n", 'def total(items, code=None):\n    """Grand total including tax."""\n')

    # Turn 5
    r.prompt("Update the README to describe the codes and bulk pricing.")
    r.edit("README.md", "Checkout maths for the shop.\n",
           "Checkout maths for the shop.\n\n- Codes: SAVE10, SAVE15, WELCOME5 (case-insensitive)\n"
           "- Bulk: 5% off any line of 10 or more\n- All arithmetic in integer cents\n")
    r.say("README updated.")

    transcript = r.save()
    return home, project, transcript
