"""test_companion_convert_progress.py - conversion progress in the companion (roadmap P3-11).

fm-saxml is replaced through ``_convert_command`` by a small Python script (written to tmp_path)
that prints ``--progress json`` lines like fm-saxml's, plus ordinary output, and writes the
docs.json the job looks for. It can wait on a gate file, so a test can poll the job mid-build,
and it can act like an fm-saxml from before ``--progress`` (typer refuses the option, exit 2).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import companion_server as cs

FAKE_FM_SAXML = r'''
import json, os, sys, time

docs_root, solution, mode, gate = sys.argv[1:5]
progress = "--progress" in sys.argv

if mode == "old" and progress:
    sys.stderr.write("Usage: python -m fm_saxml build [OPTIONS] {input_xml}\n"
                     "| No such option: --progress (Possible options: --port) |\n")
    sys.exit(2)

def emit(event, stage, file, index, total, elapsed, **extra):
    if progress:
        sys.stdout.write(json.dumps(dict(event=event, stage=stage, file=file, index=index, total=total,
                                         elapsed=elapsed, **extra)) + "\n")
        sys.stdout.flush()

print("Converting Multi.xml (2 files)", flush=True)
print("{not json, just braces}", flush=True)
print('{"no_event_key": true}', flush=True)
emit("stage_start", "analyze", None, 0, 2, 0.1)
emit("file_start", "analyze", "FileA", 1, 2, 0.1)
emit("file_done", "analyze", "FileA", 1, 2, 3.2, seconds=3.1)
emit("file_start", "analyze", "FileB", 2, 2, 3.2)
if gate != "-":
    deadline = time.monotonic() + 20
    while not os.path.exists(gate) and time.monotonic() < deadline:
        time.sleep(0.05)
emit("file_done", "analyze", "FileB", 2, 2, 3.7, seconds=0.5)
emit("stage_done", "analyze", None, 2, 2, 3.7, seconds=3.6)
emit("done", None, None, 0, 0, 3.8)
sys.stderr.write("1 warning\n")

out = os.path.join(docs_root, solution)
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "docs.json"), "w", encoding="utf-8") as f:
    json.dump({"solution": solution, "kind": "batch", "files": []}, f)
print("Done: " + out, flush=True)
'''


@pytest.fixture(autouse=True)
def clean_state(monkeypatch, tmp_path):
    """No jobs, no default docs root, and an fm-saxml assumed to take --progress."""
    monkeypatch.delenv(cs.DOCS_ROOT_ENV, raising=False)
    monkeypatch.setattr(cs, "AFM_ROOT", str(tmp_path / "standalone" / "agentic-fm"))
    monkeypatch.setattr(cs, "_convert_progress_supported", True)
    with cs._convert_lock:
        cs._convert_jobs.clear()


@pytest.fixture
def server():
    """Base URL of a companion server on a free port."""
    httpd = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.CompanionHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.fixture
def export(tmp_path):
    path = tmp_path / "Multi.xml"
    path.write_text("<FMSaveAsXML/>", encoding="utf-8")
    return str(path)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Install the fake fm-saxml. Returns its settings: mode ("new"/"old"), gate path, and the calls."""
    script = tmp_path / "fake_fm_saxml.py"
    script.write_text(FAKE_FM_SAXML, encoding="utf-8")
    settings = {"mode": "new", "gate": "-", "calls": []}

    def command(export_path, docs_root, solution_name, **kwargs):
        progress = kwargs.get("progress", True)
        settings["calls"].append(progress)
        cmd = [sys.executable, str(script), docs_root, solution_name, settings["mode"], settings["gate"]]
        return cmd + (["--progress", "json"] if progress else [])

    monkeypatch.setattr(cs, "_convert_command", command)
    return settings


def _request(url: str, body: dict | None = None) -> tuple:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _start(base: str, export: str, tmp_path) -> str:
    status, body = _request(f"{base}/convert", {"solution_name": "Multi", "export_path": export,
                                                "docs_root": str(tmp_path / "docs")})
    assert status == 202, body
    return body["job_id"]


def _wait(base: str, job_id: str, until=lambda job: job["done"], timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, job = _request(f"{base}/convert/{job_id}")
        assert status == 200, job
        if until(job):
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id}: condition not met in {timeout}s")


# -- parsing -------------------------------------------------------------------------

def test_parse_progress_line():
    line = json.dumps({"event": "file_start", "stage": "read", "file": "A", "index": 1, "total": 2,
                       "elapsed": 0.3}) + "\n"
    assert cs._parse_progress_line(line)["file"] == "A"
    assert cs._parse_progress_line("  " + line) is not None
    for other in ("Converting Multi.xml\n", "{not json}\n", '{"stage": "read"}\n', "[1, 2]\n",
                  '"{\\"event\\": 1}"\n', "\n", ""):
        assert cs._parse_progress_line(other) is None, other


@pytest.mark.parametrize("event, message", [
    ({"event": "stage_start", "stage": "analyze", "file": None, "index": 0, "total": 12, "elapsed": 1.0},
     "Analyzing: 12 file(s)"),
    ({"event": "file_start", "stage": "analyze", "file": "Invoices", "index": 3, "total": 12, "elapsed": 9.0},
     "Analyzing 3/12 Invoices"),
    ({"event": "file_done", "stage": "analyze", "file": "Invoices", "index": 3, "total": 12, "elapsed": 13.2,
      "seconds": 4.2}, "Analyzing 3/12 Invoices (4.2s)"),
    ({"event": "stage_done", "stage": "write_raw", "file": None, "index": 12, "total": 12, "elapsed": 40.0,
      "seconds": 37.5}, "Writing raw XML done (37.5s)"),
    ({"event": "done", "stage": None, "file": None, "index": 0, "total": 0, "elapsed": 41.25},
     "fm-saxml done (41.2s)"),
    ({"event": "file_start", "stage": "new_stage", "file": "A", "index": 1, "total": 1, "elapsed": 0},
     "New stage 1/1 A"),
    ({"event": "something_else", "stage": "read", "file": "A", "index": 1, "total": 2},
     "something_else Reading 1/2 A"),
])
def test_progress_message(event, message):
    assert cs._progress_message(event) == message


def test_progress_rejected():
    assert cs._progress_rejected("| No such option: --progress (Possible options: --port) |")
    assert cs._progress_rejected("\x1b[31mError: No such option: --progress\x1b[0m")
    assert not cs._progress_rejected("No such option: --colour")
    assert not cs._progress_rejected("Error: Invalid value for '--progress': 'xml'")
    assert not cs._progress_rejected("")


def test_convert_command_asks_for_progress():
    cmd = cs._convert_command("in.xml", "docs", "Sol")
    assert cmd[cmd.index("--progress") + 1] == "json"
    assert "--progress" not in cs._convert_command("in.xml", "docs", "Sol", progress=False)


# -- the job's progress block --------------------------------------------------------

def test_progress_block_while_running_and_after(server, export, tmp_path, fake):
    gate = tmp_path / "gate"
    fake["gate"] = str(gate)
    job_id = _start(server, export, tmp_path)

    job = _wait(server, job_id, until=lambda j: j["progress"].get("file") == "FileB")
    assert job["state"] == "running" and job["done"] is False
    assert job["progress"] == {
        "stage": "analyze", "file": "FileB", "index": 2, "total": 2, "elapsed": 3.2,
        "files": [{"stage": "analyze", "file": "FileA", "seconds": 3.1}],
    }

    gate.write_text("go", encoding="utf-8")
    job = _wait(server, job_id)
    assert job["state"] == "succeeded", job
    assert job["progress"] == {
        "stage": None, "file": None, "index": 0, "total": 0, "elapsed": 3.8,
        "files": [{"stage": "analyze", "file": "FileA", "seconds": 3.1},
                  {"stage": "analyze", "file": "FileB", "seconds": 0.5}],
    }
    assert fake["calls"] == [True]


def test_new_job_starts_with_empty_progress(server, export, tmp_path, fake, monkeypatch):
    release = threading.Event()
    real_run = cs._run_command_streaming

    def held_run(cmd, **kwargs):
        assert release.wait(20), "test never released the job"
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(cs, "_run_command_streaming", held_run)
    job_id = _start(server, export, tmp_path)
    try:
        status, job = _request(f"{server}/convert/{job_id}")
        assert status == 200 and job["progress"] == {}
    finally:
        release.set()
    assert _wait(server, job_id)["state"] == "succeeded"


def test_one_readable_line_per_event_and_no_raw_json(server, export, tmp_path, fake, caplog):
    with caplog.at_level(logging.INFO, logger="companion_server"):
        job_id = _start(server, export, tmp_path)
        job = _wait(server, job_id)
    assert job["state"] == "succeeded", job
    messages = [r.getMessage() for r in caplog.records]
    progress_lines = [m for m in messages if m.startswith(f"convert {job_id}: ")
                      and not m.startswith(f"convert {job_id}: {sys.executable}")]
    assert progress_lines == [f"convert {job_id}: {line}" for line in (
        "Analyzing: 2 file(s)",
        "Analyzing 1/2 FileA",
        "Analyzing 1/2 FileA (3.1s)",
        "Analyzing 2/2 FileB",
        "Analyzing 2/2 FileB (0.5s)",
        "Analyzing done (3.6s)",
        "fm-saxml done (3.8s)",
    )]
    assert not any('"event"' in m for m in messages)
    # Other output is logged as before: stdout at info, stderr at warning.
    assert any(m.endswith("] Converting Multi.xml (2 files)") for m in messages)
    assert any(m.endswith("] {not json, just braces}") for m in messages)
    assert any(m.endswith('] {"no_event_key": true}') for m in messages)
    assert any(r.levelno == logging.WARNING and r.getMessage().endswith("] 1 warning") for r in caplog.records)


def test_progress_lines_are_left_out_of_the_log_tail(server, export, tmp_path, fake, monkeypatch):
    monkeypatch.setattr(cs, "_find_output_folder", lambda *a: "")   # fail after the build, to get a log_tail
    job = _wait(server, _start(server, export, tmp_path))
    assert job["state"] == "failed"
    assert "Converting Multi.xml" in job["log_tail"] and "{not json, just braces}" in job["log_tail"]
    assert '"event"' not in job["log_tail"]
    assert job["progress"]["files"][-1]["file"] == "FileB"


# -- an fm-saxml without --progress --------------------------------------------------

def test_old_fm_saxml_still_converts(server, export, tmp_path, fake, caplog):
    fake["mode"] = "old"
    with caplog.at_level(logging.INFO, logger="companion_server"):
        job = _wait(server, _start(server, export, tmp_path))
    assert job["state"] == "succeeded", job
    assert job["exit_code"] == 0 and job["progress"] == {}
    assert fake["calls"] == [True, False]       # refused once, then run without --progress
    assert cs._convert_progress_supported is False
    assert any("has no --progress" in r.getMessage() for r in caplog.records)

    # The next conversion goes straight to the command without --progress.
    job = _wait(server, _start(server, export, tmp_path))
    assert job["state"] == "succeeded" and job["progress"] == {}
    assert fake["calls"] == [True, False, False]


def test_other_exit_code_2_is_not_retried(server, export, tmp_path, monkeypatch):
    calls = []

    def command(*args, **kwargs):
        calls.append(kwargs.get("progress", True))
        return [sys.executable, "-c", "import sys; sys.stderr.write('Error: Invalid value for INPUT_XML'); sys.exit(2)"]

    monkeypatch.setattr(cs, "_convert_command", command)
    job = _wait(server, _start(server, export, tmp_path))
    assert job["state"] == "failed" and job["exit_code"] == 2
    assert calls == [True] and cs._convert_progress_supported is True


# -- quiet polling -------------------------------------------------------------------

def test_convert_polls_are_logged_at_debug_only(server, export, tmp_path, fake, caplog):
    job_id = _start(server, export, tmp_path)
    _wait(server, job_id)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="companion_server"):
        assert _request(f"{server}/convert/{job_id}")[0] == 200
        assert _request(f"{server}/convert/unknownjob")[0] == 404
        assert _request(f"{server}/health")[0] == 200
        assert _request(f"{server}/nowhere")[0] == 404
    access = [r.getMessage() for r in caplog.records if "HTTP/1." in r.getMessage()]
    assert not any("GET /convert/" in m for m in access), access
    assert any('"GET /health ' in m for m in access) and any('"GET /nowhere ' in m for m in access)

    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="companion_server"):
        _request(f"{server}/convert/{job_id}")
    assert any(r.levelno == logging.DEBUG and f'"GET /convert/{job_id} ' in r.getMessage()
               for r in caplog.records)


@pytest.mark.parametrize("command, path, code, level", [
    ("GET", "/convert/3f9c0a1b2c4d", "200", logging.DEBUG),
    ("GET", "/convert/nope", "404", logging.DEBUG),
    ("POST", "/progress", "200", logging.DEBUG),     # P3-12 logs its own line per event
    ("POST", "/progress", "400", logging.INFO),
    ("POST", "/convert", "202", logging.INFO),
    ("GET", "/health", "200", logging.INFO),
])
def test_access_log_level(command, path, code, level, caplog):
    handler = cs.CompanionHandler.__new__(cs.CompanionHandler)
    handler.command, handler.path, handler.client_address = command, path, ("127.0.0.1", 50000)
    with caplog.at_level(logging.DEBUG, logger="companion_server"):
        handler.log_message('"%s" %s %s', f"{command} {path} HTTP/1.1", code, "-")
    assert [r.levelno for r in caplog.records] == [level]


def test_post_convert_is_still_logged_at_info(server, export, tmp_path, fake, caplog):
    with caplog.at_level(logging.INFO, logger="companion_server"):
        job_id = _start(server, export, tmp_path)
    _wait(server, job_id)
    assert any('"POST /convert ' in r.getMessage() and r.levelno == logging.INFO for r in caplog.records)
