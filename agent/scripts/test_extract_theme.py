"""test_extract_theme.py - extract_theme.py from fm-saxml docs (roadmap P2-10).

- On the golden fixture's two custom themes, the port's four files match what the exploder
  path produced (golden/extract_theme/*.sha256). The manifest is compared in key-sorted form:
  the model lists a theme's color palette as swatch1, swatch2... where the theme XML lists
  them in reverse, the only difference.
- Theme metadata comes from the model, the CSS from the theme's raw XML.
- Choosing a theme: default, display name, docId, none; --list; errors.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

import extract_theme as et
import golden_fixture as gf
import solution_context as sc

pytestmark = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                reason="fm-saxml (fm-saxml-converter) is not installed")
SCRIPT = Path(et.__file__).resolve()
FILES = ["theme-classes.json", "theme-manifest.json", "theme-web.css", "theme.css"]


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    return gf.build_docs(tmp_path_factory.mktemp("docs").resolve())


@pytest.fixture
def ctx(docs):
    return sc.load_solution(str(docs))


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})


def _digest(name: str, text: str) -> str:
    if name == "theme-manifest.json":
        text = json.dumps(json.loads(text), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("theme", gf.THEMES)
def test_matches_exploder_output(docs, tmp_path, theme):
    r = _run("-s", str(docs), "--theme", theme, "-o", str(tmp_path))
    assert r.returncode == 0, r.stderr
    golden = (gf.GOLDEN / "extract_theme" / f"{theme}.sha256").read_text(encoding="utf-8")
    want = dict(reversed(line.split("  ", 1)) for line in golden.splitlines())
    got = {n: _digest(n, (tmp_path / n).read_text(encoding="utf-8")) for n in FILES}
    assert got == want


def test_metadata_from_model_css_from_raw_xml(ctx):
    theme = et.pick_theme(ctx, "Apex Blue Copy")
    root = ET.fromstring(ctx.raw_xml(theme.doc_id))
    css = et.extract_css(root)
    theme.color_palette = {"swatch1": "#123456"}
    theme.layout_builder = {"kVerticalFieldSpacing": "7"}
    manifest = et.build_theme_manifest(root, css, theme)
    assert manifest["theme"] == {"name": "Apex Blue Copy", "id": 4,
                                 "internalName": theme.name, "baseTheme": "com.filemaker.theme.apex_blue"}
    assert manifest["colorPalette"] == {"swatch1": "#123456"}
    assert manifest["layoutBuilder"] == {"kVerticalFieldSpacing": 7}
    assert len(manifest["namedStyles"]) == len(theme.named_styles)
    assert any(s["css"] for s in manifest["namedStyles"])                 # cut from the theme's CSS


def test_pick_theme(ctx):
    assert et.pick_theme(ctx).display_name == "Vibrant Custom"           # defaultTheme
    assert et.pick_theme(ctx, "apex blue copy").fmp_id == "4"
    vibrant = et.pick_theme(ctx)
    assert et.pick_theme(ctx, vibrant.doc_id) is vibrant
    with pytest.raises(sc.SolutionContextError, match="(?s)No theme 'Nope'.*Apex Blue Copy"):
        et.pick_theme(ctx, "Nope")


def test_layout_classes_from_raw_xml(ctx):
    classes = et.scan_layout_classes(ctx)
    assert classes["minimal_edit_box"]["layouts"] == ["EverythingBagel - ID 1"]


def test_list_and_errors(docs, tmp_path):
    r = _run("-s", str(docs), "--list")
    assert r.returncode == 0 and "Vibrant Custom (ID 3" in r.stdout and "(default)" in r.stdout
    r = _run("-s", str(docs), "--theme", "Nope")
    assert r.returncode == 1 and "No theme 'Nope'" in r.stderr
    r = _run("-s", str(tmp_path / "missing"), "--docs-root", str(tmp_path))
    assert r.returncode == 1 and "ERROR:" in r.stderr
