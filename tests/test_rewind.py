import sys
from pathlib import Path

import pytest

from rewind.bisect import bisect
from rewind.demo import PRICING_V0, README_V0, build_demo
from rewind.reconstruct import ABSENT, apply_hunks, build_timeline, file_key
from rewind.sandbox import Sandbox, choose_root
from rewind.transcript import load_session, resolve_session


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    base = tmp_path_factory.mktemp("demo")
    home, project, transcript = build_demo(base)
    session = load_session(transcript, home)
    return home, project, session, build_timeline(session, use_git=False)


def test_hunks_roundtrip():
    a = "one\ntwo\nthree\nfour\nfive\n"
    b = "one\ntwo\nTHREE\nfour\nfive\nsix\n"
    from rewind.demo import _patch

    p = _patch(a, b)
    assert apply_hunks(a, p) == b
    assert apply_hunks(b, p, reverse=True) == a


def test_hunks_tolerate_shifted_lines():
    from rewind.demo import _patch

    a = "x\ny\nz\n"
    b = "x\nY\nz\n"
    shifted = "header\nmore\n" + a
    assert apply_hunks(shifted, _patch(a, b)) == "header\nmore\n" + b


def test_session_parsed(demo):
    _home, _project, s, _tl = demo
    assert s.title == "Integer-cents pricing refactor"
    assert len(s.prompts) == 5
    assert sum(1 for st in s.steps if st.kind == "edit") == 12
    assert s.calls and s.calls[-1].usage.context > s.calls[0].usage.context


def test_resolve_latest(demo):
    home, project, s, _tl = demo
    assert resolve_session("latest", str(project), home).stem == s.id
    assert resolve_session("5e55", str(project), home).stem == s.id


def test_reconstruction_exact_and_matches_disk(demo):
    _home, project, _s, tl = demo
    for fh in tl.files.values():
        if fh.path.endswith("README.md"):
            # No snapshot of its start: rebuilt by undoing the edit from disk.
            assert [m.after_q for m in fh.mutations] == ["inferred"]
        else:
            assert fh.fidelity() == 1.0, fh.path
        assert fh.matches_disk, fh.path
    pricing = tl.files[file_key(str(project / "shop" / "pricing.py"))]
    assert pricing.baseline == PRICING_V0
    readme = tl.files[file_key(str(project / "README.md"))]
    assert readme.baseline == README_V0  # recovered by undoing the edit from disk
    money = tl.files[file_key(str(project / "shop" / "money.py"))]
    assert money.baseline is ABSENT


def test_external_change_detected(demo):
    _home, _project, _s, tl = demo
    ext = tl.external_changes
    assert len(ext) == 1
    assert ext[0].path.endswith("pricing.py")
    assert any("sort_imports" in s.text for s in ext[0].suspects)


def test_bisect_finds_the_flooring_edit(demo, tmp_path):
    _home, project, s, tl = demo
    root = choose_root(tl)
    assert Path(root) == project
    sb = Sandbox(tl, root, dest=str(tmp_path / "sb"))
    sb.create()
    try:
        res = bisect(tl, sb, f'"{sys.executable}" check_discount.py', sb.cwd_for(s.cwd))
    finally:
        sb.remove()
    assert res.status == "found", res.message
    assert "off = cents * pct // 100" in res.culprit.step.input["new_string"]
    assert res.culprit.step.turn == 2
    assert res.runs <= 8
    # Working tree untouched.
    assert "// 100" in (project / "shop" / "pricing.py").read_text(encoding="utf-8")


def test_state_zero_restores_baseline(demo, tmp_path):
    _home, project, _s, tl = demo
    sb = Sandbox(tl, str(project), dest=str(tmp_path / "sb0"))
    sb.create()
    sb.apply(0)
    assert (sb.dir / "shop" / "pricing.py").read_text(encoding="utf-8") == PRICING_V0
    assert not (sb.dir / "shop" / "money.py").exists()
    assert not (sb.dir / "tests" / "test_bulk.py").exists()
    sb.remove()


def test_crlf_files_keep_crlf(tmp_path):
    from rewind.reconstruct import forward
    from rewind.model import Step

    step = Step(index=0, kind="edit", timestamp="t", turn=1, tool="Edit",
                input={"old_string": "b\n", "new_string": "B\n"})
    assert forward(step, "a\nb\nc\n") == "a\nB\nc\n"


def test_blame_attributes_lines_to_prompts(demo):
    _home, project, _s, tl = demo
    from rewind.blame import blame

    target = str(project / "shop" / "pricing.py")
    lines = blame(target, [tl])
    by_text = {ln.text.strip(): ln.source for ln in lines}
    assert by_text["off = cents * pct // 100"].turn == 2
    assert "integer cents" in by_text["off = cents * pct // 100"].prompt
    assert by_text["BULK_PCT = 5"].turn == 3
    assert by_text["# money helpers"].kind == "outside"  # written by the import sorter, not an agent edit
    assert by_text['"""Checkout pricing."""'].kind == "origin"
