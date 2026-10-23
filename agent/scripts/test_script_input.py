"""test_script_input.py - fm_xml_to_snippet.py and saxmlpreview.py take a script from fm-saxml docs
(roadmap P2-11, P2-12).

- fm_xml_to_snippet.py: the snippet of every fixture script, read by name from the docs, is
  byte-identical to the one made from the exploder's script file. The docs are built without
  scrubbing here: by default fm-saxml redacts the export's sample credentials in raw/ too.
- saxmlpreview.py: the line map of every fixture script, read from the docs, matches the
  golden output (golden/saxmlpreview/).
- Both: --script by name or docId, --doc-id; a name matching several scripts or none fails
  with the candidates; an XML file still works.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

import fm_xml_to_snippet as fx
import golden_fixture as gf
import saxmlpreview
import solution_context as sc

pytestmark = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                reason="fm-saxml (fm-saxml-converter) is not installed")
SNIPPET = Path(fx.__file__).resolve()
PREVIEW = Path(saxmlpreview.__file__).resolve()
EXPLODED = sorted((gf.FIXTURE / "xml_parsed" / "scripts" / gf.SOLUTION).rglob("*.xml"))


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    return gf.build_docs(tmp_path_factory.mktemp("docs").resolve())


@pytest.fixture(scope="module")
def unscrubbed(tmp_path_factory):
    return gf.build_docs(tmp_path_factory.mktemp("raw").resolve(), scrub=False)


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True,
                          encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})


def _name(path: Path) -> str:
    return path.stem.rsplit(" - ID ", 1)[0]


def test_fixture_has_scripts():
    assert [_name(p) for p in EXPLODED] == ["Explode XML", "Get agentic-fm path", "Just Beep", "OneOfEverything"]


# -- fm_xml_to_snippet.py (P2-11) ---------------------------------------------------------

@pytest.mark.parametrize("path", EXPLODED, ids=_name)
def test_snippet_byte_identical_to_exploder_file(unscrubbed, path):
    r = _run(SNIPPET, "-s", str(unscrubbed), "--script", _name(path))
    assert r.returncode == 0, r.stderr
    assert r.stdout == fx.translate_script(path) + "\n"           # print() adds the newline


def test_snippet_by_doc_id_to_file(docs, tmp_path):
    out = tmp_path / "beep.xml"
    r = _run(SNIPPET, "-s", str(docs), "--doc-id", "script:Just Beep", "-o", str(out))
    assert r.returncode == 0, r.stderr
    assert "<fmxmlsnippet" in out.read_text(encoding="utf-8") and "Beep" in out.read_text(encoding="utf-8")


def test_snippet_xml_file_still_works(tmp_path):
    out = tmp_path / "out.xml"
    r = _run(SNIPPET, str(EXPLODED[2]), str(out))
    assert r.returncode == 0, r.stderr
    assert out.read_text(encoding="utf-8") == fx.translate_script(EXPLODED[2])


# -- saxmlpreview.py (P2-12) --------------------------------------------------------------

@pytest.mark.parametrize("path", EXPLODED, ids=_name)
def test_preview_matches_golden(unscrubbed, path):
    r = _run(PREVIEW, "-s", str(unscrubbed), "--script", _name(path))
    assert r.returncode == 0, r.stderr
    assert gf.compare(f"saxmlpreview/{path.stem}.txt", r.stdout) is None


# fm-saxml scrubs raw/ by default. In the fixture that changes two lines of OneOfEverything:
# the AI account name is a credential (redacted on purpose), and the RAG step's parameter
# *type* "RAGAccountName" is redacted with its value, so the step can't be decoded (an
# fm-saxml bug: structure is not a credential). When fm-saxml fixes the second, this fails:
# drop that pair.
SCRUBBED_LINES = [
    ('21\tConfigure AI Account [ Account Name: "accountNameGoesHere" ; Model Provider: OpenAI ]',
     '21\tConfigure AI Account [ Account Name: "[REDACTED:api_key#1]" ; Model Provider: OpenAI ]'),
    ('26\tConfigure RAG Account [ RAG Account Name: "RAGAccountName" ]',
     '26\tConfigure RAG Account  [ … unsupported SaXML shape … ]'),
]


def test_preview_from_scrubbed_docs(docs):
    r = _run(PREVIEW, "-s", str(docs), "--script", "OneOfEverything")
    assert r.returncode == 0, r.stderr
    golden = (gf.GOLDEN / "saxmlpreview" / "OneOfEverything - ID 1.txt").read_text(encoding="utf-8")
    changed = [(a, b) for a, b in zip(golden.splitlines(), r.stdout.splitlines()) if a != b]
    assert changed == SCRUBBED_LINES


def test_preview_golden_files_are_frozen():
    assert {f"saxmlpreview/{p.stem}.txt" for p in EXPLODED} <= gf.FROZEN


# -- both: choosing the script --------------------------------------------------------

@pytest.mark.parametrize("tool", [SNIPPET, PREVIEW], ids=["snippet", "preview"])
def test_lookup_errors(docs, tmp_path, tool):
    r = _run(tool, "-s", str(docs), "--script", "Beep")              # no exact name: suggestions
    assert r.returncode == 1 and "No script 'Beep'" in r.stderr and "script:Just Beep" in r.stderr
    r = _run(tool, "--script", "Just Beep")
    assert r.returncode == 1 and "-s <solution>" in r.stderr
    r = _run(tool, "-s", str(docs))
    assert r.returncode == 1 and "--script <name>" in r.stderr
    r = _run(tool, str(EXPLODED[2]), "-s", str(docs), "--script", "Just Beep")
    assert r.returncode == 2 and "not both" in r.stderr
    r = _run(tool)
    assert r.returncode == 2


def test_several_scripts_with_one_name(docs):
    ctx = sc.load_solution(str(docs))
    twin = ctx.scripts["script:Just Beep"].model_copy(update={"doc_id": "script:Just Beep#2"})
    ctx.scripts["script:Just Beep#2"] = twin
    with pytest.raises(sc.SolutionContextError, match="2 scripts in EverythingBagel are named 'Just Beep'"):
        ctx.pick("script", "Just Beep")
