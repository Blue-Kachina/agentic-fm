"""test_trace_build.py - trace.py build from fm-saxml references (roadmap P2-06).

- On the golden fixture, the ported build gives the golden xref.index plus/minus the
  differences listed in GOLDEN_DIFFERENCES, each with its reason.
- The mapping rules (source kinds, locations, contexts, skipped references) on small,
  made-up reference records.
- The command line: --file on a multi-file build, a clear error without docs, and query /
  dead still working on the new xref.index.
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
BAGEL = Path(gf.REPO_ROOT) / "agent" / "fixtures" / "fm_saxml" / "Bagel"

# Golden (exploder path) row -> ported row; None = row the port adds or drops. Each with its reason.
GOLDEN_DIFFERENCES = [
    # Script lines: the old build counted lines of the sanitized text, where a calc can span
    # several lines; the port counts steps, as Script Workspace and saxmlpreview.py do.
    ("script|Explode XML (ID 4)|line 59: Perform Script|script|Get agentic-fm path|",
     "script|Explode XML (ID 4)|line 31: Perform Script|script|Get agentic-fm path|"),
    # A summary field uses the field it summarises; the old build read only formulas and auto-enter.
    (None, "field_calc|EverythingBagel::summaryField|summary|field|EverythingBagel::numberField|same table"),
    # Value lists on layout objects: layout summaries never recorded them, so the list looked unused.
    (None, "layout|EverythingBagel (ID 1)|value list control|value_list|HardcodedValueList|"),
    (None, "layout|EverythingBagel (ID 1)|value list control|value_list|HardcodedValueList|"),
    # A value list's source field and TO: the old reader looked for <ValueListReference>, which the
    # exploder doesn't write (golden README, known bug 2), so it found none.
    (None, "value_list|ValuesFromField (ID 2)|primary field|field|EverythingBagel::textField|"),
    (None, "value_list|ValuesFromField (ID 2)|table occurrence|table_occurrence|EverythingBagel|"),
]


def _run(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})


@pytest.fixture
def built(tmp_path):
    """(shadow repo root, docs root) with the golden fixture's export converted."""
    if importlib.util.find_spec("fm_saxml") is None:
        pytest.skip("fm-saxml is not installed")
    tmp = tmp_path.resolve()
    root = gf._shadow_root(tmp)
    gf.build_docs(tmp / "docs")
    return root, tmp / "docs"


# -- golden --------------------------------------------------------------------------

@needs_fm_saxml
def test_build_matches_golden_with_listed_differences(built):
    root, docs = built
    r = _run(root, "agent/scripts/trace.py", "build", "-s", "EverythingBagel", "--docs-root", str(docs))
    assert r.returncode == 0, r.stderr
    new = (root / "agent" / "context" / "EverythingBagel" / "xref.index").read_text(encoding="utf-8")
    golden = (gf.GOLDEN / "trace" / "xref.index").read_text(encoding="utf-8")

    expected = sorted(golden.splitlines())
    for old, ported in GOLDEN_DIFFERENCES:
        if old is not None:
            expected.remove(old)
        if ported is not None:
            expected.append(ported)
    assert sorted(new.splitlines()) == sorted(expected)
    assert "22 total references" in r.stdout


@needs_fm_saxml
def test_query_and_dead_read_the_new_index(built):
    root, docs = built
    assert _run(root, "agent/scripts/trace.py", "build", "-s", "EverythingBagel", "--docs-root", str(docs)).returncode == 0
    q = _run(root, "agent/scripts/trace.py", "query", "-s", "EverythingBagel", "-t", "script", "-n", "Just Beep")
    assert q.returncode == 0 and "EverythingBagel (ID 1), button script" in q.stdout
    d = _run(root, "agent/scripts/trace.py", "dead", "-s", "EverythingBagel", "-t", "scripts")
    assert d.returncode == 0 and "OneOfEverything" in d.stdout and "Just Beep" not in d.stdout


# -- command line --------------------------------------------------------------------

@needs_fm_saxml
def test_build_one_file_of_a_batch(tmp_path):
    tmp = tmp_path.resolve()
    root = gf._shadow_root(tmp)
    shutil.copytree(BAGEL, tmp / "exports" / "Bagel")
    r = _run(root, "-m", "fm_saxml", "build", str(tmp / "exports" / "Bagel"), "--docs-root", str(tmp / "docs"),
             "--solution", "Bagel")
    assert r.returncode == 0, r.stdout + r.stderr
    r = _run(root, "agent/scripts/trace.py", "build", "-s", "Bagel", "--docs-root", str(tmp / "docs"),
             "--file", "EverythingBagel")
    assert r.returncode == 0, r.stderr
    assert (root / "agent" / "context" / "EverythingBagel" / "xref.index").is_file()


@needs_fm_saxml
def test_build_docs_folder_into_named_context(built):
    root, docs = built
    r = _run(root, "agent/scripts/trace.py", "build", "-s", str(docs / "EverythingBagel"), "--context", "EB_Adapter")
    assert r.returncode == 0, r.stderr
    assert (root / "agent" / "context" / "EB_Adapter" / "xref.index").is_file()
    assert "Building xref.index for: EB_Adapter" in r.stdout


def test_build_without_docs_is_one_message(tmp_path):
    root = gf._shadow_root(tmp_path.resolve())
    env = {k: v for k, v in os.environ.items() if k != "FM_SAXML_DOCS_ROOT"}
    r = subprocess.run([sys.executable, "agent/scripts/trace.py", "build", "-s", "Nope"], cwd=root,
                       capture_output=True, text=True, encoding="utf-8", env={**env, "PYTHONUTF8": "1"})
    assert r.returncode == 1
    assert r.stderr.startswith("ERROR: ") and "Traceback" not in r.stderr


# -- mapping rules -------------------------------------------------------------------

def _ref(source, target, rel="usesField", role=None, raw=None, confidence="exact", via=None, external=None,
         target_type=None):
    return NS(source_doc_id=source, source_entity_type=source.split(":", 1)[0], target_doc_id=target,
              target_entity_type=target_type or target.split(":", 1)[0], relationship_type=rel, role=role, raw_text=raw,
              confidence=confidence, via=via, external_target=external)


class FakeContext:
    """Just what trace's build reads from a SolutionContext."""

    def __init__(self, refs):
        self.references = refs
        f = lambda name, qn: NS(name=name, qualified_name=qn, fmp_id="7")  # noqa: E731
        self._c = {
            "script": {"script:Print": NS(name="Print", fmp_id="12"), "script:Open": NS(name="Open", fmp_id="3")},
            "scriptStep": {"scriptStep:Print:0004": NS(index=4, name="Set Field", script_doc_id="script:Print")},
            "layout": {"layout:Invoice": NS(name="Invoice", fmp_id="5")},
            "layoutObject": {"layoutObject:Invoice::9": NS(layout_doc_id="layout:Invoice",
                                                           table_occurrence_doc_id="to:Invoice Lines")},
            "field": {"field:Invoices::Total": f("Total", "Invoices::Total"),
                      "field:Invoices::Tax": f("Tax", "Invoices::Tax"),
                      "field:Lines::Amount": f("Amount", "Lines::Amount")},
            "to": {}, "valueList": {}, "menu": {},
            "relationship": {"relationship:R": NS(left_table_occurrence_doc_id="to:Invoices",
                                                  right_table_occurrence_doc_id="to:Invoice Lines")},
            "customFunction": {"customFunction:Round2": NS(name="Round2", fmp_id="2")},
            "customMenu": {"customMenu:Main": NS(name="Main", fmp_id="1")},
        }
        self.layouts = self._c["layout"]
        self.scripts = self._c["script"]

    def collection(self, kind):
        return self._c[kind]

    def get(self, doc_id):
        for c in self._c.values():
            if doc_id in c:
                return c[doc_id]
        return None


def _rows(*refs):
    return ["|".join(x) for x in trace.build_xrefs(FakeContext(list(refs)))]


def test_script_step_field_via_to_and_line():
    assert _rows(_ref("scriptStep:Print:0004", "field:Lines::Amount", role="target", raw="Invoice Lines::Amount")) == [
        'script|Print (ID 12)|line 5: Set Field|field|Lines::Amount|via TO "Invoice Lines"']


def test_field_calc_same_table_auto_enter_and_lookup():
    assert _rows(
        _ref("field:Invoices::Total", "field:Invoices::Tax", role="calculation", raw="Invoices::Tax"),
        _ref("field:Invoices::Total", "field:Lines::Amount", role="autoEnter", raw="Lines::Amount"),
        _ref("field:Invoices::Tax", "field:Lines::Amount", role="lookupSource"),
    ) == ["field_calc|Invoices::Total|calc|field|Invoices::Tax|same table",
          "field_auto|Invoices::Total|auto-enter|field|Lines::Amount|",
          "field_auto|Invoices::Tax|lookup|field|Lines::Amount|"]


def test_layout_object_placement_trigger_and_rollup():
    rows = _rows(
        _ref("layoutObject:Invoice::9", "field:Lines::Amount", role="display"),
        _ref("layout:Invoice", "field:Lines::Amount", role="display"),            # fm-saxml's rollup: skipped
        _ref("layout:Invoice", "script:Open", rel="triggersScript", role="OnRecordLoad"),
        _ref("layoutObject:Invoice::9", "script:Print", rel="triggersScript", role="button"),
        _ref("layout:Invoice", "field:Invoices::Tax", role="scriptTriggerParameter", raw="Invoices::Tax"),
    )
    assert rows == ['layout|Invoice (ID 5)|field placement|field|Lines::Amount|via TO "Invoice Lines"',
                    "layout|Invoice (ID 5)|trigger: OnRecordLoad|script|Open|",
                    "layout|Invoice (ID 5)|button script|script|Print|",
                    "layout|Invoice (ID 5)|trigger parameter|field|Invoices::Tax|"]


def test_relationship_custom_function_file_and_menu():
    assert _rows(
        _ref("relationship:R", "field:Invoices::Total", role="left"),
        _ref("relationship:R", "field:Lines::Amount", role="rightSort"),
        _ref("customFunction:Round2", "field:Invoices::Tax", role="calculation", raw="Invoices::Tax"),
        _ref("fileOptions:file", "script:Open", rel="triggersScript", role="OnFirstWindowOpen"),
        _ref("customMenu:Main", "script:Print", rel="usesScript", role="menuItem"),
    ) == ["relationship|Invoices→Invoice Lines|join field|field|Invoices::Total|left side",
          "relationship|Invoices→Invoice Lines|sort field|field|Lines::Amount|right side",
          "file|File|trigger: OnFirstWindowOpen|script|Open|",
          "custom_func|Round2 (ID 2)|calc body|field|Invoices::Tax|",
          "custom_menu|Main (ID 1)|menu item|script|Print|"]


def test_external_unresolved_and_by_name():
    ext = NS(name="Total", target_doc_id="field:Orders::Total", base_table="Orders", data_source="Sales")
    assert _rows(
        _ref("scriptStep:Print:0004", "ds:Sales/field:Orders::Total", role="target", confidence="external",
             external=ext, target_type="field"),
        _ref("scriptStep:Print:0004", "script:Gone", rel="usesScript", role="callee", confidence="unresolved"),
        _ref("scriptStep:Print:0004", "script:Open", rel="usesScript", role="callee", via="byName"),
    ) == ['script|Print (ID 12)|line 5: Set Field|field|Orders::Total|external file "Sales"',
          "script|Print (ID 12)|line 5: Set Field|script|Gone|unresolved",
          "script|Print (ID 12)|line 5: Set Field|script|Open|by name"]


def test_structural_and_other_targets_are_skipped():
    assert _rows(
        _ref("layout:Invoice", "to:Invoices", rel="basedOnTableOccurrence"),
        _ref("table:Invoices", "field:Invoices::Total", rel="contains"),
        _ref("account:Admin", "privilegeSet:Full", rel="assignedPrivilegeSet"),
        _ref("layout:Invoice", "layoutObject:Invoice::9", rel="contains"),
    ) == []


def test_pipes_are_escaped_in_the_index():
    assert trace._escape_pipe("a|b") == "a\\|b"
