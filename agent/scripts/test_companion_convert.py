"""test_companion_convert.py - POST /convert and GET /convert/<job id> (roadmap P3-04).

A real companion server runs on a free port in a thread. The success test converts the
``agent/fixtures/fm_saxml/Multi/`` export with the installed fm-saxml (skipped when it isn't
installed); the failure tests replace the fm-saxml command or the runner, so they need nothing.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import companion_server as cs

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MULTI = os.path.join(REPO_ROOT, "agent", "fixtures", "fm_saxml", "Multi")

needs_fm_saxml = pytest.mark.skipif(importlib.util.find_spec("fm_saxml") is None,
                                    reason="fm-saxml (fm-saxml-converter) is not installed")


@pytest.fixture
def server():
    """Base URL of a companion server on a free port."""
    with cs._convert_lock:
        cs._convert_jobs.clear()
    httpd = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.CompanionHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture(autouse=True)
def no_default_docs_root(monkeypatch, tmp_path):
    """No $FM_SAXML_DOCS_ROOT, and agentic-fm not a harness submodule (whatever this checkout is)."""
    monkeypatch.delenv(cs.DOCS_ROOT_ENV, raising=False)
    monkeypatch.setattr(cs, "AFM_ROOT", str(tmp_path / "standalone" / "agentic-fm"))
    # An fm-saxml without --progress makes the companion remember that (P3-11); start each test fresh.
    monkeypatch.setattr(cs, "_convert_progress_supported", True)
    # Job records (P3-23) go to a test state folder, never the real per-user one
    monkeypatch.setenv("COMPANION_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture(autouse=True)
def stop_viewers():
    """Viewers served by a test (webmcp builds) stop when it ends."""
    yield
    cs._stop_viewer_servers()


def _make_afm(base, git_file: bool = True, parent: str = "tools", gitmodules: bool = True) -> str:
    """An agentic-fm root at <base>/harness/<parent>/agentic-fm, laid out like a (non-)submodule."""
    harness = base / "harness"
    root = harness / parent / "agentic-fm"
    root.mkdir(parents=True)
    if git_file:
        (root / ".git").write_text("gitdir: ../../.git/modules/tools/agentic-fm\n", encoding="utf-8")
    else:
        (root / ".git").mkdir()
    if gitmodules:
        (harness / ".gitmodules").write_text('[submodule "tools/agentic-fm"]\n', encoding="utf-8")
    return str(root)


@pytest.fixture
def export(tmp_path):
    """A copy of the Multi fixture folder, so nothing is written next to the repo's copy."""
    dest = tmp_path / "exports" / "Multi"
    shutil.copytree(MULTI, dest)
    return str(dest)


def _request(url: str, body: dict | None = None, method: str | None = None) -> tuple:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _wait(base: str, job_id: str, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, job = _request(f"{base}/convert/{job_id}")
        assert status == 200, job
        if job["done"]:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


def _python(code: str) -> list:
    return [sys.executable, "-c", code]


# -- start and poll ------------------------------------------------------------------

def test_start_returns_job_at_once_and_server_keeps_serving(server, export, tmp_path, monkeypatch):
    release = threading.Event()
    real_run = cs._run_command_streaming

    def slow_run(cmd, **kwargs):
        assert release.wait(20), "test never released the job"
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(cs, "_run_command_streaming", slow_run)
    monkeypatch.setattr(cs, "_convert_command", lambda *a, **k: _python("pass"))

    started = time.monotonic()
    status, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                                  "docs_root": str(tmp_path / "docs")})
    assert time.monotonic() - started < 5
    assert status == 202, body
    assert body["success"] is True and body["state"] in ("queued", "running")
    assert body["status_url"] == f"/convert/{body['job_id']}"

    # The conversion is still blocked, and the server still answers.
    status, job = _request(f"{server}/convert/{body['job_id']}")
    assert status == 200 and job["state"] in ("queued", "running") and job["done"] is False
    assert _request(f"{server}/health")[0] == 200

    # A second conversion of the same solution is refused while the first runs.
    status, dup = _request(f"{server}/convert", {"solution_name": "multi", "export_path": export,
                                                 "docs_root": str(tmp_path / "docs")})
    assert status == 409 and dup["job_id"] == body["job_id"]

    release.set()
    job = _wait(server, body["job_id"])
    assert job["state"] == "failed"     # `pass` writes no docs, so there is nothing to summarise
    assert "no docs.json" in job["error"]


@needs_fm_saxml
def test_success_reports_counts_and_lands_where_solution_context_looks(server, export, tmp_path):
    docs_root = tmp_path / "docs"
    status, body = _request(f"{server}/convert", {"solution_name": "MultiSol", "export_path": export,
                                                  "docs_root": str(docs_root)})
    assert status == 202, body
    job = _wait(server, body["job_id"])
    assert job["state"] == "succeeded", job
    assert job["exit_code"] == 0 and job["error"] == ""
    assert job["kind"] == "batch"
    assert job["counts"]["files"] == 2
    assert job["counts"]["tables"] == 2 and job["counts"]["fields"] == 10 and job["counts"]["layouts"] == 3
    assert [f["name"] for f in job["files"]] == ["FileA", "FileB"]
    assert isinstance(job["warning_count"], int) and isinstance(job["warnings"], list)
    assert os.path.isfile(os.path.join(job["output_path"], "docs.json"))
    assert job["html"] is False and job["viewer_path"] == ""

    import solution_context as sc
    ctx = sc.load_solution("MultiSol", docs_root=str(docs_root), file="FileB")
    assert ctx.name == "FileB"


@needs_fm_saxml
def test_docs_root_falls_back_to_env(server, export, tmp_path, monkeypatch):
    monkeypatch.setenv(cs.DOCS_ROOT_ENV, str(tmp_path / "envdocs"))
    status, body = _request(f"{server}/convert", {"solution_name": "MultiSol", "export_path": export})
    assert status == 202, body
    job = _wait(server, body["job_id"])
    assert job["state"] == "succeeded", job
    assert job["docs_root"] == str(tmp_path / "envdocs")


# -- failure -------------------------------------------------------------------------

def test_non_zero_exit_is_failed(server, export, tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "_convert_command",
                        lambda *a, **k: _python("import sys; sys.stderr.write('boom: bad export'); sys.exit(3)"))
    _, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                             "docs_root": str(tmp_path / "docs")})
    job = _wait(server, body["job_id"])
    assert job["state"] == "failed" and job["exit_code"] == 3
    assert "code 3" in job["error"] and "boom: bad export" in job["log_tail"]


def test_crash_is_failed_and_server_keeps_serving(server, export, tmp_path, monkeypatch):
    def crash(cmd, **kwargs):
        raise OSError("cannot start fm-saxml")

    real_run = cs._run_command_streaming
    monkeypatch.setattr(cs, "_run_command_streaming", crash)
    _, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                             "docs_root": str(tmp_path / "docs")})
    job = _wait(server, body["job_id"])
    assert job["state"] == "failed" and "cannot start fm-saxml" in job["error"]

    assert _request(f"{server}/health")[0] == 200
    monkeypatch.setattr(cs, "_convert_command", lambda *a, **k: _python("import sys; sys.exit(1)"))
    monkeypatch.setattr(cs, "_run_command_streaming", real_run)
    status, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                                  "docs_root": str(tmp_path / "docs")})
    assert status == 202     # the crashed job released the slot and the solution
    job = _wait(server, body["job_id"])
    assert job["state"] == "failed" and job["exit_code"] == 1


# -- validation ----------------------------------------------------------------------

def test_bad_requests(server, export, tmp_path):
    assert _request(f"{server}/convert", {"export_path": export})[0] == 400
    status, body = _request(f"{server}/convert", {"solution_name": "M", "export_path": str(tmp_path / "nope")})
    assert status == 400 and "not found" in body["error"]
    status, body = _request(f"{server}/convert", {"solution_name": "M", "export_path": export})
    assert status == 400 and cs.DOCS_ROOT_ENV in body["error"]
    req = urllib.request.Request(f"{server}/convert", data=b"{not json", method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=10)
    assert exc.value.code == 400
    assert _request(f"{server}/convert/doesnotexist")[0] == 404


def test_export_path_sent_unchanged_outside_wsl(server, export, tmp_path, monkeypatch):
    """P3-06 through /convert: on Windows and macOS (CI) fm-saxml gets the path as sent."""
    def boom(*args):
        raise AssertionError("WSL translation entered")

    seen = {}
    monkeypatch.setattr(cs, "_wsl_translate_path", boom)
    monkeypatch.setattr(cs, "_convert_command", lambda path, root, name, **k: seen.update(path=path) or _python("pass"))
    _, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                             "docs_root": str(tmp_path / "docs")})
    _wait(server, body["job_id"])
    assert seen["path"] == export


# -- html / webmcp switches ----------------------------------------------------------

def test_convert_command_switches():
    base = cs._convert_command("in.xml", "docs", "Sol")
    assert "--html" not in base and "--webmcp" not in base
    assert cs._convert_command("in.xml", "docs", "Sol", html=True)[len(base):] == ["--html"]
    assert cs._convert_command("in.xml", "docs", "Sol", html=True, webmcp=True)[len(base):] == ["--html", "--webmcp"]


@pytest.mark.parametrize("sent, expected", [
    ({}, {"html": False, "webmcp": False}),
    ({"html": True}, {"html": True, "webmcp": False}),
    ({"webmcp": True}, {"html": True, "webmcp": True}),     # --webmcp turns on --html, as in fm-saxml
    ({"html": False, "webmcp": False}, {"html": False, "webmcp": False}),
])
def test_switches_reach_fm_saxml_and_the_job(server, export, tmp_path, monkeypatch, sent, expected):
    seen = {}
    monkeypatch.setattr(cs, "_convert_command", lambda *a, **k: seen.update(k) or _python("pass"))
    status, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                                  "docs_root": str(tmp_path / "docs"), **sent})
    assert status == 202, body
    job = _wait(server, body["job_id"])
    assert seen == expected
    assert {k: job[k] for k in expected} == expected


@pytest.mark.parametrize("field, value", [("html", "true"), ("html", 1), ("webmcp", None), ("webmcp", "yes")])
def test_switches_must_be_booleans(server, export, tmp_path, field, value):
    status, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export,
                                                  "docs_root": str(tmp_path / "docs"), field: value})
    assert status == 400 and field in body["error"] and "true or false" in body["error"]
    with cs._convert_lock:
        assert not cs._convert_jobs     # nothing was queued


@needs_fm_saxml
def test_webmcp_build_reports_the_viewer(server, export, tmp_path, caplog):
    status, body = _request(f"{server}/convert", {"solution_name": "MultiSol", "export_path": export,
                                                  "docs_root": str(tmp_path / "docs"), "webmcp": True})
    assert status == 202, body
    with caplog.at_level(logging.INFO, logger="companion_server"):
        job = _wait(server, body["job_id"])
    assert job["state"] == "succeeded", job
    assert job["html"] is True and job["webmcp"] is True
    assert job["viewer_path"] == os.path.join(job["output_path"], "index.html")
    assert os.path.isfile(job["viewer_path"])
    assert os.path.isdir(os.path.join(job["output_path"], "_viewer", "data"))
    # served at once, without a browser, and announced in the log
    assert job["viewer_url"].startswith("http://127.0.0.1:") and job["viewer_url"].endswith("/index.html")
    with urllib.request.urlopen(job["viewer_url"], timeout=10) as resp:
        assert resp.status == 200
    assert any(job["viewer_url"] in r.getMessage() and "WebMCP viewer for MultiSol" in r.getMessage()
               for r in caplog.records)
    _, health = _request(f"{server}/health")
    assert health["viewers"] == [{"output_path": job["output_path"], "url": job["viewer_url"]}]


@needs_fm_saxml
def test_html_build_is_not_served(server, export, tmp_path):
    _, body = _request(f"{server}/convert", {"solution_name": "MultiSol", "export_path": export,
                                             "docs_root": str(tmp_path / "docs"), "html": True})
    job = _wait(server, body["job_id"])
    assert job["state"] == "succeeded", job
    assert job["viewer_path"] and job["viewer_url"] == ""
    assert cs._served_viewers() == []


# -- serving the viewer --------------------------------------------------------------

def _webmcp_folder(base) -> str:
    """A folder fm-saxml's has_webmcp accepts: index.html and the viewer's webmcp.js."""
    from fm_saxml.render.html.viewer import VIEWER_DIR
    (base / VIEWER_DIR).mkdir(parents=True)
    (base / "index.html").write_text("<html></html>", encoding="utf-8")
    (base / VIEWER_DIR / "webmcp.js").write_text("", encoding="utf-8")
    return str(base)


@needs_fm_saxml
def test_viewer_server_is_reused_per_folder(tmp_path):
    first, second = _webmcp_folder(tmp_path / "a"), _webmcp_folder(tmp_path / "b")
    url = cs._serve_viewer(first)
    assert url and cs._serve_viewer(first) == url      # a rebuild of the same folder
    other = cs._serve_viewer(second)
    assert other and other != url                       # the port was taken by the first
    assert {v["url"] for v in cs._served_viewers()} == {url, other}
    cs._stop_viewer_servers()
    assert cs._served_viewers() == []
    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=2)


@needs_fm_saxml
def test_folder_without_webmcp_is_not_served(tmp_path, caplog):
    (tmp_path / "index.html").write_text("<html></html>", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        assert cs._serve_viewer(str(tmp_path)) == ""
    assert any("no viewer with WebMCP" in r.getMessage() for r in caplog.records)


def test_fm_saxml_without_serve_is_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "fm_saxml.serve", None)    # import fails
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        assert cs._serve_viewer(str(tmp_path)) == ""
    assert any("fm-saxml serve" in r.getMessage() for r in caplog.records)


# -- /explode alias ------------------------------------------------------------------

def test_explode_is_a_deprecated_alias(server, export, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(cs, "_convert_command", lambda *a, **k: _python("import sys; sys.exit(2)"))
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        status, body = _request(f"{server}/explode", {
            "solution_name": "Multi", "export_file_path": export,
            "repo_path": REPO_ROOT, "docs_root": str(tmp_path / "docs"),
        })
    assert status == 202, body
    assert body["deprecated"] is True and "/convert" in body["deprecation"]
    assert any("deprecated" in r.getMessage() for r in caplog.records)
    job = _wait(server, body["job_id"])
    assert job["state"] == "failed" and job["exit_code"] == 2

    status, body = _request(f"{server}/explode", {"solution_name": "Multi"})
    assert status == 400 and body["deprecated"] is True


# -- docs root -----------------------------------------------------------------------

def test_harness_detection(tmp_path):
    root = _make_afm(tmp_path / "ok")
    assert cs._harness_root(root) == str(tmp_path / "ok" / "harness")
    assert cs._harness_root(_make_afm(tmp_path / "gitdir", git_file=False)) == ""      # a clone, not a submodule
    assert cs._harness_root(_make_afm(tmp_path / "nomods", gitmodules=False)) == ""
    assert cs._harness_root(_make_afm(tmp_path / "notools", parent="vendor")) == ""


def test_docs_root_order(tmp_path, monkeypatch):
    afm = _make_afm(tmp_path)
    exports = tmp_path / "harness" / "exports"
    monkeypatch.setattr(cs, "AFM_ROOT", afm)

    # c) harness default; exports/ is created
    assert not exports.exists()
    assert cs._resolve_docs_root() == (str(exports), "harness")
    assert exports.is_dir()

    # b) the env var beats the harness default
    monkeypatch.setenv(cs.DOCS_ROOT_ENV, str(tmp_path / "envdocs"))
    assert cs._resolve_docs_root() == (str(tmp_path / "envdocs"), "env")

    # a) the request beats the env var
    assert cs._resolve_docs_root(str(tmp_path / "reqdocs")) == (str(tmp_path / "reqdocs"), "request")

    # d) nothing applies
    monkeypatch.delenv(cs.DOCS_ROOT_ENV)
    monkeypatch.setattr(cs, "AFM_ROOT", _make_afm(tmp_path / "clone", git_file=False))
    assert cs._resolve_docs_root() == ("", "")


def test_startup_message(tmp_path, monkeypatch):
    assert "none set" in cs._docs_root_startup_message()
    monkeypatch.setattr(cs, "AFM_ROOT", _make_afm(tmp_path))
    assert cs._docs_root_startup_message() == (
        f"Docs root: {tmp_path / 'harness' / 'exports'} (harness exports/)")


def test_convert_uses_harness_exports_by_default(server, export, tmp_path, monkeypatch):
    monkeypatch.setattr(cs, "AFM_ROOT", _make_afm(tmp_path))
    monkeypatch.setattr(cs, "_convert_command", lambda *a, **k: _python("pass"))
    status, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export})
    assert status == 202, body
    job = _wait(server, body["job_id"])
    assert job["docs_root"] == str(tmp_path / "harness" / "exports")

    # the same through the /explode alias, with the old body
    status, body = _request(f"{server}/explode", {"solution_name": "Multi", "export_file_path": export,
                                                  "repo_path": REPO_ROOT})
    assert status == 202, body
    assert _wait(server, body["job_id"])["docs_root"] == str(tmp_path / "harness" / "exports")


def test_no_docs_root_message_names_all_three_ways(server, export):
    status, body = _request(f"{server}/convert", {"solution_name": "Multi", "export_path": export})
    assert status == 400
    assert "docs_root" in body["error"] and cs.DOCS_ROOT_ENV in body["error"] and "submodule" in body["error"]


# -- idle auto-shutdown --------------------------------------------------------------

def test_idle_shutdown_waits_for_unfinished_jobs(monkeypatch):
    monkeypatch.setattr(cs, "_convert_jobs", {})
    monkeypatch.setattr(cs, "_seconds_since_activity", lambda: 600.0)    # idle timeout long past
    assert cs._idle_shutdown_due(300)

    for state in ("queued", "running"):
        cs._convert_jobs["job1"] = {"job_id": "job1", "state": state, "done": False}
        assert not cs._idle_shutdown_due(300), state

    cs._convert_jobs["job1"].update(state="succeeded", done=True)
    assert cs._idle_shutdown_due(300)

    monkeypatch.setattr(cs, "_seconds_since_activity", lambda: 10.0)     # not idle long enough
    assert not cs._idle_shutdown_due(300)


def test_idle_watchdog_does_not_stop_server_during_a_job(monkeypatch):
    """The watchdog loop itself, with sleep faked: it shuts down only after the job is done."""
    monkeypatch.setattr(cs, "_convert_jobs", {"job1": {"job_id": "job1", "state": "running", "done": False}})
    monkeypatch.setattr(cs, "_seconds_since_activity", lambda: 600.0)
    checks = []

    def fake_sleep(seconds):
        checks.append(seconds)
        if len(checks) == 3:     # the job finishes after the watchdog has passed it over twice
            cs._convert_jobs["job1"].update(state="succeeded", done=True)

    class FakeServer:
        shutdowns = 0

        def shutdown(self):
            FakeServer.shutdowns += 1

    monkeypatch.setattr(cs.time, "sleep", fake_sleep)
    cs._idle_watchdog(FakeServer(), 300)
    assert FakeServer.shutdowns == 1 and len(checks) == 3


# -- warnings report -----------------------------------------------------------------

def test_read_warnings(tmp_path):
    report = tmp_path / "warnings.md"
    report.write_text(
        "---\ntitle: Warnings\n---\n\n# Warnings\n\n_2 warning(s) total_\n\n"
        "| Code | Entity | Message | Detail |\n|---|---|---|---|\n"
        "| `UNRESOLVED_REF` | script:Go Home | Perform Script target not found | id 12 |\n"
        "| `EMPTY_CALC` | _ | Calculation is empty |  |\n",
        encoding="utf-8")
    count, rows = cs._read_warnings(str(report))
    assert count == 2
    assert rows == [
        {"code": "UNRESOLVED_REF", "entity": "script:Go Home", "message": "Perform Script target not found"},
        {"code": "EMPTY_CALC", "entity": "", "message": "Calculation is empty"},
    ]
    assert cs._read_warnings(str(tmp_path / "missing.md")) == (0, [])
