"""test_golden_fixture.py - the exploder-path golden fixture and its compare helper (roadmap P2-05).

- Today's exploder-path tools still produce golden/ on the fixture's inputs, so the golden
  files are reproducible and every port is compared with output these tools really give.
- compare() reports differences as a unified diff, and ignores order where asked.
- The fixture's export converts with the installed fm-saxml and holds the same scripts and
  layouts as the exploded tree, so ported tools read the same solution the golden came from.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

import golden_fixture as gf


def test_tools_reproduce_golden():
    outputs = gf.run_tools()
    produced = set(outputs)
    on_disk = {p.relative_to(gf.GOLDEN).as_posix() for p in gf.GOLDEN.rglob("*") if p.is_file()}
    assert produced == on_disk, f"missing: {sorted(on_disk - produced)}, new: {sorted(produced - on_disk)}"
    diffs = [d for name, text in outputs.items() if (d := gf.compare(name, text))]
    assert not diffs, "\n".join(diffs) + "\nIf intended: python3 agent/scripts/golden_fixture.py --update"


def test_compare_shows_differences(tmp_path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert gf.compare("a.txt", "one\r\ntwo\r\nthree\r\n", golden_dir=tmp_path) is None
    diff = gf.compare("a.txt", "one\nTWO\nthree\n", golden_dir=tmp_path)
    assert "--- golden/a.txt" in diff and "-two" in diff and "+TWO" in diff


def test_compare_ignores_order_when_asked(tmp_path):
    (tmp_path / "a.index").write_text("b|2\na|1\n", encoding="utf-8")
    assert gf.compare("a.index", "a|1\nb|2\n", golden_dir=tmp_path) is not None
    assert gf.compare("a.index", "a|1\nb|2\n", sort_lines=True, golden_dir=tmp_path) is None
    assert "trace/xref.index" in gf.UNORDERED


def test_compare_missing_golden(tmp_path):
    assert "no golden file" in gf.compare("nope.txt", "x", golden_dir=tmp_path)


@pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None, reason="fm-saxml is not installed")
def test_fm_saxml_conversion_matches_exploded_tree(tmp_path):
    import solution_context as sc

    ctx = sc.load_solution(str(gf.build_docs(tmp_path)))

    def exploded(kind: str) -> list[str]:
        return sorted(p.stem.rsplit(" - ID ", 1)[0] for p in (gf.FIXTURE / "xml_parsed" / kind / gf.SOLUTION).glob("*.xml"))

    assert sorted(s.name for s in ctx.scripts.values()) == exploded("scripts")
    assert sorted(lay.name for lay in ctx.layouts.values()) == exploded("layouts")
    assert ctx.raw_xml(ctx.pick("script", "Just Beep").doc_id).startswith("<Script")
