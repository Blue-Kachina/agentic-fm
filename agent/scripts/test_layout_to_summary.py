"""test_layout_to_summary.py - layout_to_summary.py from fm-saxml docs (roadmap P2-09).

- On the golden fixture, the port gives the golden summaries byte for byte.
- Model-backed keys come from the model, and the XML's value stays where the model can't
  resolve a reference.
- The display styles the old map lacked (checkbox set, concealed edit box).
- Choosing layouts: docId, exact name, partial name, none; the command line.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import golden_fixture as gf
import layout_to_summary as lts
import solution_context as sc

pytestmark = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                reason="fm-saxml (fm-saxml-converter) is not installed")
SCRIPT = Path(lts.__file__).resolve()
GOLDEN_NAMES = [f"{name}.json" for name in gf.LAYOUTS]


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    return gf.build_docs(tmp_path_factory.mktemp("docs").resolve())


@pytest.fixture
def ctx(docs):
    return sc.load_solution(str(docs))


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})


# -- golden --------------------------------------------------------------------------

def test_matches_golden(docs, tmp_path):
    r = _run("-s", str(docs), "-o", str(tmp_path))
    assert r.returncode == 0, r.stderr
    assert sorted(p.name for p in tmp_path.glob("*.json")) == GOLDEN_NAMES
    for name in GOLDEN_NAMES:
        assert gf.compare(f"layout_to_summary/{name}", (tmp_path / name).read_text(encoding="utf-8")) is None


def test_golden_files_are_frozen():
    assert {f"layout_to_summary/{n}" for n in GOLDEN_NAMES} <= gf.FROZEN


# -- model and raw XML ------------------------------------------------------------------

def _portal_and_button(summary):
    objects = summary["parts"][1]["objects"]
    portal = next(o for o in objects if o["type"] == "Portal")
    bar = next(o for o in objects if o["type"] == "Button Bar")
    return portal, bar["buttons"][2]


def test_model_keys_come_from_the_model(ctx):
    lay = ctx.pick("layout", "EverythingBagel")
    model = lts.LayoutModel(ctx, lay)
    portal = model.objects["17"]
    portal.bounds.top = 999.0
    portal.portal.rows = 3
    lay.width = 800
    summary = lts.parse_layout(ET.fromstring(ctx.raw_xml(lay.doc_id)), model)
    p, button = _portal_and_button(summary)
    assert (summary["width"], p["bounds"][0], p["visibleRows"]) == (800, 999, 3)
    assert p["relatedTO"] == "EverythingBagel__EVERYTHINGBAGEL__selfJoin"
    assert (button["script"], button["scriptId"]) == ("Just Beep", 2)


def test_xml_value_stays_when_the_model_cannot_resolve(ctx):
    lay = ctx.pick("layout", "EverythingBagel")
    model = lts.LayoutModel(ctx, lay)
    model.objects["23"].button_script_doc_id = "script:No such script"
    summary = lts.parse_layout(ET.fromstring(ctx.raw_xml(lay.doc_id)), model)
    assert _portal_and_button(summary)[1]["script"] == "Just Beep"


def test_xml_only_keys_come_from_raw_xml(ctx):
    lay = ctx.pick("layout", "EverythingBagel")
    summary, size = lts.summarise(ctx, lay)
    assert size == len(ctx.raw_xml(lay.doc_id).encode("utf-8"))
    objects = summary["parts"][1]["objects"]
    assert any(o.get("styleName") == "centered_text" for o in objects)        # LocalCSS
    assert any(o.get("text") == "textField" for o in objects)                 # static text
    assert any("conditions" in o for o in objects)                            # hide condition


@pytest.mark.parametrize("code, style", [("3", "checkbox"), ("7", "concealedEditBox"), ("4", "radioButtons")])
def test_display_styles(code, style):
    el = ET.fromstring(f'<LayoutObject id="1" type="Edit Box"><Field><FieldReference id="5" name="f">'
                       f'<TableOccurrenceReference name="T"/></FieldReference><Display Style="{code}"/>'
                       f'</Field></LayoutObject>')
    assert lts.parse_layout_object(el)["displayStyle"] == style


# -- choosing layouts and the command line ----------------------------------------------

def test_select_layouts(ctx):
    names = lambda key: [lay.name for lay in lts.select_layouts(ctx, key)]
    assert names(None) == ["CustomMenuSetLayout", "EverythingBagel"]
    assert names("layout:EverythingBagel") == ["EverythingBagel"]
    assert names("everythingbagel") == ["EverythingBagel"]            # exact name, any case
    assert names("menu") == ["CustomMenuSetLayout"]                   # partial
    with pytest.raises(sc.SolutionContextError, match="No layout 'Nope'"):
        lts.select_layouts(ctx, "Nope")


def test_print_one_layout(docs):
    r = _run("-s", str(docs), "--layout", "CustomMenuSetLayout", "--print")
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["layout"] == "CustomMenuSetLayout"


def test_errors(docs, tmp_path):
    r = _run("--layout", "x")
    assert r.returncode == 2 and "-s/--solution" in r.stderr
    r = _run("-s", str(tmp_path / "missing"), "--docs-root", str(tmp_path))
    assert r.returncode == 1 and "ERROR:" in r.stderr
    r = _run("-s", str(docs), "--layout", "Nope")
    assert r.returncode == 1 and "No layout 'Nope'" in r.stderr


def test_layout_xml_file_still_works(ctx, tmp_path):
    lay = ctx.pick("layout", "CustomMenuSetLayout")
    xml = tmp_path / "layout.xml"
    xml.write_text(ctx.raw_xml(lay.doc_id), encoding="utf-8")
    r = _run(str(xml))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout)["layout"] == "CustomMenuSetLayout"
