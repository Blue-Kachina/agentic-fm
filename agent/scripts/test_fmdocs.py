"""test_fmdocs.py - links into the fm-saxml docs site, docs age, changes, active solution (P5-13, P5-17, P5-18, P6-07).

Built from the golden fixture (agent/fixtures/fm_saxml/Bagel) and the two-file fixture
(agent/fixtures/fm_saxml/Multi) with the installed fm-saxml, with and without --webmcp.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import fmdocs

pytestmark = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                reason="fm-saxml (fm-saxml-converter) is not installed")

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "fm_saxml"
BAGEL_XML = FIXTURES / "Bagel" / "EverythingBagel.xml"
MULTI = FIXTURES / "Multi"


def _build(source: Path, root: Path, solution: str, *flags: str) -> Path:
    r = subprocess.run([sys.executable, "-m", "fm_saxml", "build", str(source), "--docs-root", str(root),
                        "--solution", solution, "--force", *flags],
                       capture_output=True, text=True, encoding="utf-8", env={**os.environ, "PYTHONUTF8": "1"})
    assert r.returncode == 0, r.stdout + r.stderr
    return root / solution


@pytest.fixture(scope="module")
def docs(tmp_path_factory):
    """Docs root with Bagel and Multi built with --webmcp."""
    root = tmp_path_factory.mktemp("docs")
    _build(BAGEL_XML, root, "Bagel", "--webmcp")
    _build(MULTI, root, "Multi", "--webmcp")
    return root


def _main(*args: str) -> int:
    return fmdocs.main(list(args))


# -- links ----------------------------------------------------------------------------

def test_link_by_type_and_name_uses_the_catalog_path(docs, capsys):
    assert _main("link", "-s", "Bagel", "--docs-root", str(docs), "--type", "script", "--name", "just beep") == 0
    url = capsys.readouterr().out.strip()
    assert url.startswith("file:///") and url.endswith("/Bagel/index.html#/Scripts/Just%20Beep.md")


def test_link_by_doc_id(docs, capsys):
    assert _main("link", "-s", "Bagel", "--docs-root", str(docs), "--doc-id", "script:Just Beep") == 0
    assert capsys.readouterr().out.strip().endswith("#/Scripts/Just%20Beep.md")


def test_multi_file_name_lists_candidates_until_a_file_is_given(docs, capsys):
    assert _main("link", "-s", "Multi", "--docs-root", str(docs), "--type", "field", "--name", "PrimaryKey") == 3
    out = capsys.readouterr().out
    assert "#/FileA/Fields/FileA/PrimaryKey.md" in out and "#/FileB/Fields/FileB/PrimaryKey.md" in out
    assert _main("link", "-s", "Multi", "--docs-root", str(docs), "--file", "FileB",
                 "--type", "field", "--name", "PrimaryKey") == 0
    assert capsys.readouterr().out.strip().endswith("/Multi/index.html#/FileB/Fields/FileB/PrimaryKey.md")


def test_solution_named_after_a_batch_file_picks_that_file(docs, capsys):
    assert _main("link", "-s", "FileB", "--docs-root", str(docs), "--type", "field", "--name", "PrimaryKey") == 0
    assert "#/FileB/Fields/FileB/PrimaryKey.md" in capsys.readouterr().out


def test_unknown_object_is_an_error(docs, capsys):
    assert _main("link", "-s", "Bagel", "--docs-root", str(docs), "--type", "script", "--name", "Nope") == 1
    assert "No page for script 'Nope'" in capsys.readouterr().err


def test_page_url_encodes_each_segment_like_the_viewer():
    assert fmdocs.page_url("B", "Scripts/a b/c#d?.md") == "B#/Scripts/a%20b/c%23d%3F.md"


def test_front_matter_index_without_webmcp(tmp_path):
    root = _build(BAGEL_XML, tmp_path, "Bagel", "--html")
    rows = fmdocs.page_index(root)
    assert {"docId": "script:Just Beep", "type": "script", "name": "Just Beep",
            "path": "Scripts/Just Beep.md", "file": ""} in rows


def test_no_viewer_is_a_clear_error(tmp_path):
    root = _build(BAGEL_XML, tmp_path, "Bagel")
    with pytest.raises(fmdocs.FmDocsError, match="no HTML viewer"):
        fmdocs.base_url(root, health=None)


# -- base and results -----------------------------------------------------------------

def test_base_prefers_the_viewer_the_companion_serves(docs):
    health = {"viewers": [{"output_path": str(docs / "Multi"), "url": "http://127.0.0.1:9999/index.html"},
                          {"output_path": str(docs / "Bagel"), "url": "http://127.0.0.1:8766/index.html"}]}
    assert fmdocs.base_url(docs / "Bagel", health) == ("http://127.0.0.1:8766/index.html", "served")
    assert fmdocs.base_url(docs / "Multi" / "FileA", health)[0] == "http://127.0.0.1:9999/index.html"
    url, kind = fmdocs.base_url(docs / "Bagel", None)
    assert kind == "file" and url == (docs / "Bagel" / "index.html").resolve().as_uri()


def test_results_url_round_trips_to_the_tool_call():
    args = {"docId": "field:Invoices::Total & Tax", "direction": "in"}
    url = fmdocs.results_url("B", "find_references", args, as_json=True)
    assert url.endswith("&format=json")
    q = url.split("#/results?q=", 1)[1].split("&", 1)[0]
    assert json.loads(urllib.parse.unquote(q)) == {"tool": "find_references", "args": args}


def test_results_rejects_tools_a_results_page_cannot_run(docs, capsys):
    assert _main("results", "-s", "Bagel", "--docs-root", str(docs), "--tool", "navigate_to") == 1
    assert "find_references" in capsys.readouterr().err


def test_results_rejects_arguments_the_tool_does_not_take(docs, capsys):
    # what an agent wrote in a skill eval: the link ran, and the tool found nothing
    assert _main("results", "-s", "Bagel", "--docs-root", str(docs), "--tool", "find_references",
                 "--args", '{"name": "EverythingBagel::textField"}') == 1
    err = capsys.readouterr().err
    assert "'name'" in err and "docId" in err


@pytest.mark.parametrize("tool, args, needs", [
    ("rename_impact", {}, "docId"),
    ("query", {"steps": []}, "steps"),
    ("find_references", {"direction": "incoming"}, "docId or docIds or targetType + parentDocId"),
    ("find_references", {"targetType": "field"}, "targetType + parentDocId"),
    ("what_can_access", {"type": "script"}, "account or privilegeSet"),
])
def test_results_rejects_calls_without_what_the_tool_needs(tool, args, needs):
    with pytest.raises(fmdocs.FmDocsError, match=needs.replace("+", r"\+")):
        fmdocs.results_url("B", tool, args)


def test_the_calls_in_docs_site_pass_the_check():
    text = (Path(__file__).resolve().parents[1] / "docs" / "DOCS_SITE.md").read_text(encoding="utf-8")
    calls = re.findall(r"`(\w+) (\{.*?\})`", text)
    assert len(calls) >= 9
    for tool, args in calls:
        fmdocs.results_url("B", tool, json.loads(args))


def _webmcp_tools(js: str) -> dict[str, tuple[set[str], set[str]]]:
    """{tool: (argument names, required names)} from fm-saxml's webmcp.js tool(...) calls."""
    found = {}
    for m in re.finditer(r"\btool\('(\w+)',", js):
        parts, depth, i, start, quote = [], 0, m.end(), m.end(), None
        while True:   # split tool(...)'s arguments on its top-level commas, skipping strings and comments
            c = js[i]
            if not quote and js.startswith("//", i):
                i = js.index("\n", i)
                continue
            if not quote and js.startswith("/*", i):
                i = js.index("*/", i) + 2
                continue
            if quote:
                if c == "\\":
                    i += 1
                elif c == quote:
                    quote = None
            elif c in "'\"`":
                quote = c
            elif c in "([{":
                depth += 1
            elif c in ")]}":
                if depth == 0:
                    parts.append(js[start:i])
                    break
                depth -= 1
            elif c == "," and depth == 0:
                parts.append(js[start:i])
                start = i + 1
            i += 1
        props, opts = parts[1].strip(), parts[3] if len(parts) > 3 else ""
        names, depth, quote = set(), 0, None
        for j, c in enumerate(props):   # the keys at the top level of the properties literal
            if quote:
                quote = None if c == quote and props[j - 1] != "\\" else quote
            elif c in "'\"`":
                quote = c
            elif c in "([{":
                depth += 1
                if depth == 1:
                    key_from = j + 1
            elif c in ")]}":
                depth -= 1
            elif c == "," and depth == 1:
                key_from = j + 1
            elif c == ":" and depth == 1:
                names.add(props[key_from:j].strip())
                key_from = len(props)
        names.discard("")   # a ':' in a value (a ternary) leaves an empty key
        if re.search(r"\bquery:\s*true", opts):
            names |= {"display", "title"}
        req = re.search(r"\brequired:\s*\[([^\]]*)\]", opts)
        found[m.group(1)] = (names, set(re.findall(r"'(\w+)'", req.group(1))) if req else set())
    return found


def test_result_tools_match_the_installed_fm_saxml():
    import fm_saxml
    js = (Path(fm_saxml.__file__).parent / "render" / "html" / "templates" / "webmcp.js").read_text(encoding="utf-8")
    tools = _webmcp_tools(js)
    for tool, (accepted, required) in fmdocs.RESULT_TOOLS.items():
        assert tool in tools, f"fm-saxml has no tool {tool}"
        assert (accepted, set(required)) == tools[tool], tool


# -- age ------------------------------------------------------------------------------

def test_age_from_the_export_file_named_in_the_companion_record(tmp_path):
    root = _build(BAGEL_XML, tmp_path, "Bagel")
    export = tmp_path / "export.xml"
    export.write_text("x", encoding="utf-8")
    old = time.time() - 10 * 86400
    os.utime(export, (old, old))
    (root / fmdocs.JOB_RECORD).write_text(json.dumps({"export_path": str(export), "created_at": "2020-01-01T00:00:00Z",
                                                      "finished_at": "2020-01-01T00:05:00Z"}), encoding="utf-8")
    age = fmdocs.docs_age(root)
    assert age["source"] == "export file" and age["stale"] and 9.9 < age["age_days"] < 10.1
    export.unlink()
    age = fmdocs.docs_age(root, now=datetime(2020, 1, 2, tzinfo=timezone.utc))
    assert age["source"] == "conversion request" and age["age_days"] == 1 and not age["stale"]
    assert age["converted"] == datetime(2020, 1, 1, 0, 5, tzinfo=timezone.utc)


def test_age_without_a_record_falls_back_to_the_conversion(tmp_path):
    root = _build(BAGEL_XML, tmp_path, "Bagel")
    age = fmdocs.docs_age(root, now=datetime.now(timezone.utc) + timedelta(days=8))
    assert age["source"].startswith("conversion") and age["stale"]


# -- active solution ------------------------------------------------------------------

def test_active_solution_is_the_default(docs, capsys, monkeypatch):
    monkeypatch.delenv(fmdocs.sc.DOCS_ROOT_ENV, raising=False)
    assert _main("active", "--docs-root", str(docs), "Multi") == 0
    assert "Active solution: Multi" in capsys.readouterr().out
    assert fmdocs.get_active(str(docs))["output_path"] == str((docs / "Multi").resolve())
    monkeypatch.setenv(fmdocs.sc.DOCS_ROOT_ENV, str(docs))
    assert _main("link", "--doc-id", "field:FileA::PrimaryKey") == 0
    assert "/Multi/index.html#/FileA/" in capsys.readouterr().out
    (docs / fmdocs.ACTIVE_FILE).unlink()


def test_no_active_solution_says_how_to_set_one(tmp_path, capsys):
    assert _main("link", "--docs-root", str(tmp_path), "--doc-id", "x") == 1
    assert "no active solution" in capsys.readouterr().err


# -- previous generation and changes --------------------------------------------------

def test_changes_after_renaming_a_script(tmp_path, capsys):
    root = _build(BAGEL_XML, tmp_path / "docs", "Bagel", "--webmcp")
    assert fmdocs.save_previous(root) == tmp_path / "docs" / ".previous" / "Bagel"
    assert sorted(p.name for p in (tmp_path / "docs" / ".previous" / "Bagel").iterdir()) == \
        ["docs.json", "entities.json", "snapshot.json"]

    _build(BAGEL_XML, tmp_path / "docs", "Bagel", "--webmcp")
    assert fmdocs.compare(fmdocs.previous_dir(root), root) == []

    renamed = tmp_path / "EverythingBagel.xml"
    renamed.write_text(BAGEL_XML.read_text(encoding="utf-8").replace('name="Just Beep"', 'name="Just Boop"'),
                       encoding="utf-8")
    _build(renamed, tmp_path / "docs", "Bagel", "--webmcp")
    rows = fmdocs.compare(fmdocs.previous_dir(root), root)
    changes = {(r["change"], r["docId"]) for r in rows}
    assert {("added", "script:Just Boop"), ("removed", "script:Just Beep")} <= changes
    assert not any(r["type"] == "scriptStep" for r in rows)

    assert _main("changes", "-s", "Bagel", "--docs-root", str(tmp_path / "docs")) == 0
    out = capsys.readouterr().out
    assert "added    script Just Boop" in out and "#/Scripts/Just%20Boop.md" in out


def test_a_changed_step_is_reported_under_its_script(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    base = {"schemaVersion": "1.0", "scripts": {"script:S": {"docId": "script:S", "entityType": "script", "name": "S"}},
            "scriptSteps": {"scriptStep:S:0000": {"docId": "scriptStep:S:0000", "entityType": "scriptStep",
                                                  "scriptDocId": "script:S", "fmText": "Beep",
                                                  "sourceXml": {"line": 1}}}}
    changed = json.loads(json.dumps(base))
    changed["scriptSteps"]["scriptStep:S:0000"].update(fmText="Halt Script", sourceXml={"line": 9})
    for folder, data in ((old, base), (new, changed)):
        folder.mkdir()
        (folder / "entities.json").write_text(json.dumps(data), encoding="utf-8")
    assert fmdocs.compare(old, new) == [{"file": "", "docId": "script:S", "type": "script", "name": "S",
                                         "change": "changed", "detail": "steps"}]


def test_changes_without_a_previous_generation(docs, capsys):
    assert _main("changes", "-s", "Bagel", "--docs-root", str(docs)) == 1
    assert "snapshot" in capsys.readouterr().err


def test_previous_generation_is_not_found_as_a_solution(tmp_path):
    import fm_saxml

    root = _build(BAGEL_XML, tmp_path, "Bagel")
    fmdocs.save_previous(root)
    assert fm_saxml.find_docs("Bagel", tmp_path) == root


def test_companion_keeps_the_previous_docs(tmp_path):
    import companion_server

    _build(BAGEL_XML, tmp_path, "Bagel")
    companion_server._keep_previous_docs({"job_id": "j1", "docs_root": str(tmp_path), "solution_name": "Bagel"})
    assert (tmp_path / ".previous" / "Bagel" / "entities.json").is_file()
    companion_server._keep_previous_docs({"job_id": "j2", "docs_root": str(tmp_path), "solution_name": "Other"})
