"""test_trace_query_dead.py - trace.py query and dead from fm-saxml docs (roadmap P2-07).

- On the golden fixture, every query and dead scan in golden_fixture.QUERIES / DEAD_TYPES
  prints its golden output, except the ones in GOLDEN_CHANGES, whose new output and reason
  are listed. They read the docs only: the temporary repo has no agent/context at all.
- A multi-file build: uses from other files count (dead) and show up (query), and a
  reference into another file never makes a same-named object here look used.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import golden_fixture as gf
import trace

needs_fm_saxml = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                    reason="fm-saxml (fm-saxml-converter) is not installed")
MULTI = Path(gf.REPO_ROOT) / "agent" / "fixtures" / "fm_saxml" / "Multi"

# Golden (exploder path) output that changes, with the new output and why.
GOLDEN_CHANGES = {
    # The layout's two value-list controls are references now (test_trace_build.GOLDEN_DIFFERENCES).
    "trace/query_value_list_HardcodedValueList.txt": (
        "=== References to value_list: HardcodedValueList ===\n\n"
        "LAYOUTS (2):\n"
        "  EverythingBagel (ID 1), value list control\n"
        "  EverythingBagel (ID 1), value list control\n\n"
        "Summary: 2 references across 1 source type(s)\n"),
    # The value list's source TO is a reference now (same list).
    "trace/query_table_occurrence_EverythingBagel.txt": (
        "=== References to table_occurrence: EverythingBagel ===\n\n"
        "VALUE LISTS (1):\n"
        "  ValuesFromField (ID 2), table occurrence\n\n"
        "Summary: 1 references across 1 source type(s)\n"),
    # Value list names come from the model: the exploder path's value_lists.index had blank names
    # (golden README, known bug 2), so it counted 1 list with no name. HardcodedValueList is used
    # by the layout; ValuesFromField is the one unused list.
    "trace/dead_value_lists.txt": (
        "=== Potentially unused value_lists (EverythingBagel) ===\n\n"
        "HIGH CONFIDENCE — no references found anywhere (1):\n"
        "  ValuesFromField\n\n"
        "Summary: 1 high, 0 medium unused out of 2 total value_lists\n"),
}


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "agent/scripts/trace.py", *args], cwd=root, capture_output=True,
                          text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})


@pytest.fixture(scope="module")
def golden_run(tmp_path_factory):
    """(shadow repo root without agent/context, docs root) for the golden fixture."""
    if importlib.util.find_spec("fm_saxml") is None:
        pytest.skip("fm-saxml is not installed")
    tmp = tmp_path_factory.mktemp("qd").resolve()
    root = gf._shadow_root(tmp)
    shutil.rmtree(root / "agent" / "context")         # nothing to read there: docs only
    shutil.rmtree(root / "agent" / "xml_parsed")
    gf.build_docs(tmp / "docs")
    return root, tmp / "docs"


@needs_fm_saxml
@pytest.mark.parametrize("name,kind,obj,direction", gf.QUERIES)
def test_query_matches_golden(golden_run, name, kind, obj, direction):
    root, docs = golden_run
    r = _run(root, "query", "-s", "EverythingBagel", "--docs-root", str(docs), "-t", kind, "-n", obj,
             "--direction", direction)
    assert r.returncode == 0, r.stderr
    golden_name = f"trace/query_{name}.txt"
    expected = GOLDEN_CHANGES.get(golden_name)
    out = gf._normalise(r.stdout, root)
    if expected is not None:
        assert out == expected
    else:
        assert gf.compare(golden_name, out) is None, gf.compare(golden_name, out)


@needs_fm_saxml
@pytest.mark.parametrize("kind", gf.DEAD_TYPES)
def test_dead_matches_golden(golden_run, kind):
    root, docs = golden_run
    r = _run(root, "dead", "-s", "EverythingBagel", "--docs-root", str(docs), "-t", kind)
    assert r.returncode == 0, r.stderr
    golden_name = f"trace/dead_{kind}.txt"
    expected = GOLDEN_CHANGES.get(golden_name)
    out = gf._normalise(r.stdout, root)
    if expected is not None:
        assert out == expected
    else:
        assert gf.compare(golden_name, out) is None, gf.compare(golden_name, out)


@needs_fm_saxml
def test_query_without_docs_is_one_message(golden_run):
    root, _ = golden_run
    env = {k: v for k, v in os.environ.items() if k != "FM_SAXML_DOCS_ROOT"}
    r = subprocess.run([sys.executable, "agent/scripts/trace.py", "dead", "-s", "Nope", "-t", "scripts"], cwd=root,
                       capture_output=True, text=True, encoding="utf-8", env={**env, "PYTHONUTF8": "1"})
    assert r.returncode == 1 and r.stderr.startswith("ERROR: ") and "Traceback" not in r.stderr


# -- multi-file build ----------------------------------------------------------------

@pytest.fixture(scope="module")
def multi_run(tmp_path_factory):
    if importlib.util.find_spec("fm_saxml") is None:
        pytest.skip("fm-saxml is not installed")
    tmp = tmp_path_factory.mktemp("multi").resolve()
    root = gf._shadow_root(tmp)
    shutil.copytree(MULTI, tmp / "exports" / "Multi")
    r = subprocess.run([sys.executable, "-m", "fm_saxml", "build", str(tmp / "exports" / "Multi"),
                        "--docs-root", str(tmp / "docs"), "--solution", "Multi"], capture_output=True,
                       text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    return root, tmp / "docs"


@needs_fm_saxml
def test_query_lists_uses_from_other_files(multi_run):
    root, docs = multi_run
    r = _run(root, "query", "-s", "Multi", "--docs-root", str(docs), "--file", "FileA", "-t", "field",
             "-n", "FileA::PrimaryKey")
    assert r.returncode == 0, r.stderr
    assert "OTHER FILES (2):" in r.stdout
    assert "FileB: FileB__FileA, join field" in r.stdout and "FileB: FileA, field placement" in r.stdout


@needs_fm_saxml
def test_dead_counts_uses_from_other_files(multi_run):
    root, docs = multi_run
    r = _run(root, "dead", "-s", "Multi", "--docs-root", str(docs), "--file", "FileA", "-t", "fields",
             "--verbose")
    assert r.returncode == 0, r.stderr
    for used_from_fileb in ("FileA::PrimaryKey", "FileA::CreatedBy", "FileA::ModifiedBy"):
        assert used_from_fileb not in r.stdout


def _ctx(refs, backlinks=None, scripts=("Open",)):
    """Just what query/dead's helpers read from a SolutionContext."""
    collections = {k: {} for k in ("field", "layout", "valueList", "customFunction", "to", "scriptStep",
                                   "layoutObject", "relationship", "customMenu", "table")}
    collections["script"] = {f"script:{n}": NS(name=n, fmp_id="1") for n in scripts}
    return NS(references=refs, layouts={}, scripts=collections["script"],
              collection=lambda kind: collections[kind], get=lambda d: None,
              docs=NS(backlinks=backlinks or {}))


def test_reference_into_another_file_is_not_a_local_use():
    ext = NS(name="Open", target_doc_id="script:Open", base_table=None, data_source="Other")
    ref = NS(source_doc_id="fileOptions:file", source_entity_type="fileOptions", target_doc_id="ds:Other/script:Open",
             target_entity_type="script", relationship_type="triggersScript", role="OnFirstWindowOpen",
             raw_text=None, confidence="external", via=None, external_target=ext)
    rows = trace.build_xrefs(_ctx([ref]))
    assert len(rows) == 1 and trace._is_external(rows[0])     # kept for outbound queries, not counted


def test_other_file_uses_from_backlinks():
    bl = {"script:Open": [
        NS(source_doc_id="xfile:Invoices/scriptStep:Post:0012", source_entity_type="scriptStep",
           relationship_type="usesScript", role="callee"),
        NS(source_doc_id="xfile:Invoices/layoutObject:Entry::7", source_entity_type="layoutObject",
           relationship_type="triggersScript", role="OnObjectEnter"),
        NS(source_doc_id="scriptStep:Here:0001", source_entity_type="scriptStep",      # this file's own
           relationship_type="usesScript", role="callee"),
    ]}
    rows = ["|".join(x) for x in trace.other_file_uses(_ctx([], bl))]
    assert rows == ["other_file|Invoices: Post (line 13)|Perform Script|script|Open|",
                    "other_file|Invoices: Entry|trigger: OnObjectEnter|script|Open|"]
