"""test_solution_context.py - locate and load a solution's fm-saxml docs (roadmap P2-04).

The fixtures in ``agent/fixtures/fm_saxml/Bagel/`` are Save a Copy as XML exports; they are
converted once per test session with the installed fm-saxml, as one file's docs and as a
multi-file build. Tests that need fm-saxml are skipped when it isn't installed; the
"not installed" message is tested in a subprocess that hides the package.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys

import pytest

import solution_context as sc

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BAGEL = os.path.join(REPO_ROOT, "agent", "fixtures", "fm_saxml", "Bagel")
SCRIPTS = os.path.dirname(os.path.abspath(__file__))

needs_fm_saxml = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                    reason="fm-saxml (fm-saxml-converter) is not installed")


def _build(input_path: str, root: str, solution: str) -> None:
    r = subprocess.run([sys.executable, "-m", "fm_saxml", "build", input_path, "--docs-root", root,
                        "--solution", solution],
                       capture_output=True, text=True, encoding="utf-8",
                       env={**os.environ, "PYTHONUTF8": "1"})   # Windows consoles default to cp1252
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.fixture(scope="session")
def roots(tmp_path_factory):
    """{"single": root holding EverythingBagel's docs, "batch": root holding the Bagel build}."""
    if importlib.util.find_spec("fm_saxml") is None:
        pytest.skip("fm-saxml is not installed")
    base = tmp_path_factory.mktemp("fm_saxml")
    exports = base / "exports" / "Bagel"
    shutil.copytree(BAGEL, exports)     # fm-saxml writes nothing next to the input, but keep the repo clean
    single, batch = base / "single", base / "batch"
    _build(str(exports / "EverythingBagel.xml"), str(single), "EverythingBagel")
    _build(str(exports), str(batch), "Bagel")
    return {"single": single, "batch": batch}


@pytest.fixture(autouse=True)
def no_env_root(monkeypatch):
    monkeypatch.delenv(sc.DOCS_ROOT_ENV, raising=False)


# -- found ---------------------------------------------------------------------------

@needs_fm_saxml
def test_one_file_by_name(roots):
    ctx = sc.load_solution("EverythingBagel", roots["single"])
    assert ctx.name == "EverythingBagel"
    assert ctx.batch is None
    assert ctx.schema_version.split(".")[0] == "1"
    assert "script:OneOfEverything" in ctx.scripts
    assert ctx.layouts and ctx.themes and ctx.references


@needs_fm_saxml
def test_root_from_environment(roots, monkeypatch):
    monkeypatch.setenv(sc.DOCS_ROOT_ENV, str(roots["single"]))
    assert sc.load_solution("everythingbagel").name == "EverythingBagel"


@needs_fm_saxml
def test_docs_folder_path(roots):
    folder = sc.locate("EverythingBagel", roots["single"])
    assert sc.load_solution(str(folder)).path == folder


@needs_fm_saxml
def test_raw_xml(roots):
    ctx = sc.load_solution("EverythingBagel", roots["single"])
    assert ctx.raw_xml("script:OneOfEverything").startswith("<Script")
    with pytest.raises(sc.SolutionContextError, match="raw XML"):
        ctx.raw_xml(next(iter(ctx.collection("field"))))


@needs_fm_saxml
def test_pick_by_doc_id_and_name(roots):
    ctx = sc.load_solution("EverythingBagel", roots["single"])
    by_id = ctx.pick("script", "script:OneOfEverything")
    assert ctx.pick("script", "oneofeverything") is by_id
    with pytest.raises(sc.SolutionContextError, match="Did you mean:\n  script:OneOfEverything"):
        ctx.pick("script", "OneOf")
    with pytest.raises(sc.SolutionContextError, match="No script 'Nope'"):
        ctx.pick("script", "Nope")


# -- batch root ----------------------------------------------------------------------

@needs_fm_saxml
def test_batch_root_needs_a_file(roots):
    import fm_saxml

    assert isinstance(sc.open_docs("Bagel", roots["batch"]), fm_saxml.BatchDocs)
    with pytest.raises(sc.SolutionContextError, match="--file") as e:
        sc.load_solution("Bagel", roots["batch"])
    assert "Empty, EverythingBagel" in str(e.value)


@needs_fm_saxml
def test_batch_file_by_option(roots):
    ctx = sc.load_solution("Bagel", roots["batch"], file="everythingbagel")
    assert ctx.name == "EverythingBagel"
    assert ctx.batch is not None and sorted(ctx.batch) == ["Empty", "EverythingBagel"]
    assert "script:OneOfEverything" in ctx.scripts
    with pytest.raises(sc.SolutionContextError, match="not a file of 'Bagel'"):
        sc.load_solution("Bagel", roots["batch"], file="Nope")


@needs_fm_saxml
def test_batch_file_by_solution_name(roots):
    ctx = sc.load_solution("Empty", roots["batch"])     # find_docs gives the file's own folder
    assert ctx.name == "Empty"


# -- not found -----------------------------------------------------------------------

@needs_fm_saxml
def test_no_docs_for_solution(roots):
    with pytest.raises(sc.SolutionContextError) as e:
        sc.load_solution("Nope", roots["single"])
    assert "No fm-saxml docs for 'Nope'" in str(e.value)
    assert "fm-saxml build Nope.xml" in str(e.value)


@needs_fm_saxml
def test_no_docs_root():
    with pytest.raises(sc.SolutionContextError, match=sc.DOCS_ROOT_ENV):
        sc.load_solution("EverythingBagel")


@needs_fm_saxml
def test_file_option_on_one_file(roots):
    with pytest.raises(sc.SolutionContextError, match="not a multi-file build"):
        sc.load_solution("EverythingBagel", roots["single"], file="Empty")


@needs_fm_saxml
def test_newer_contract_major(roots, tmp_path):
    folder = tmp_path / "Future"
    shutil.copytree(sc.locate("EverythingBagel", roots["single"]), folder)
    for name in ("docs.json", "entities.json"):
        data = json.loads((folder / name).read_text(encoding="utf-8"))
        data["schemaVersion"] = "99.0"
        (folder / name).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(sc.SolutionContextError, match="newer fm-saxml"):
        sc.load_solution(str(folder))


@needs_fm_saxml
def test_docs_not_in_the_contract_shape(roots, tmp_path):
    folder = tmp_path / "Edited"
    shutil.copytree(sc.locate("EverythingBagel", roots["single"]), folder)
    data = json.loads((folder / "entities.json").read_text(encoding="utf-8"))
    del next(iter(data["scriptSteps"].values()))["stepId"]       # a hand edit drops a required key
    (folder / "entities.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(sc.SolutionContextError, match="doesn't match the fm-saxml docs format") as e:
        sc.load_solution(str(folder))
    assert "stepId" in str(e.value)


# -- missing fm-saxml and the command line -------------------------------------------

def test_missing_fm_saxml_is_one_message(tmp_path):
    code = ("import sys; sys.modules['fm_saxml'] = None; sys.path.insert(0, sys.argv[1]); "
            "import solution_context as sc; sys.exit(sc.main(['-s', 'X', '--docs-root', sys.argv[2]]))")
    r = subprocess.run([sys.executable, "-c", code, SCRIPTS, str(tmp_path)],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 1
    assert "fm-saxml is not installed" in r.stderr
    assert "Traceback" not in r.stderr


@needs_fm_saxml
def test_command_line(roots):
    r = subprocess.run([sys.executable, os.path.join(SCRIPTS, "solution_context.py"), "-s", "Bagel",
                        "--docs-root", str(roots["batch"]), "--file", "EverythingBagel"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "multi-file build" in r.stdout and "File read:  EverythingBagel" in r.stdout
