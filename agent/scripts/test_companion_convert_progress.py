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
    # Job records (P3-23) go to a test state folder, never the real per-user one
    monkeypatch.setenv("COMPANION_STATE_DIR", str(tmp_path / "state"))
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
    # /health is polled too (monitors, `status`, `attach`): debug as well (P3-21)
    assert not any('"GET /health ' in m for m in access), access
    assert any('"GET /nowhere ' in m for m in access)

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
    ("GET", "/health", "200", logging.DEBUG),
    ("GET", "/healthz", "404", logging.INFO),
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


# -- stages with units (stage_progress) ----------------------------------------------

FAKE_UNITS = r'''
import json, os, sys, time

docs_root, solution, gate = sys.argv[1:4]

def emit(event, stage, file, index, total, elapsed, **extra):
    sys.stdout.write(json.dumps(dict(event=event, stage=stage, file=file, index=index, total=total,
                                     elapsed=elapsed, **extra)) + "\n")
    sys.stdout.flush()

emit("stage_start", "write_pages", None, 0, 1, 0.5)
emit("file_start", "write_pages", "FileA", 1, 1, 0.5)
emit("stage_progress", "write_pages", "FileA", 1, 1, 1.5, done=10, units=20, unit="pages")
emit("file_done", "write_pages", "FileA", 1, 1, 2.5, seconds=2.0)
emit("stage_done", "write_pages", None, 1, 1, 2.5, seconds=2.0, done=21, unit="pages")
emit("stage_start", "write_viewer", None, 0, 1, 2.5)
emit("file_start", "write_viewer", "Synapse", 1, 1, 2.5)
emit("stage_progress", "write_viewer", "Synapse", 1, 1, 47.5, done=5300, units=21193, unit="pages")
deadline = time.monotonic() + 20
while not os.path.exists(gate) and time.monotonic() < deadline:
    time.sleep(0.05)
emit("file_done", "write_viewer", "Synapse", 1, 1, 110.8, seconds=108.3)
emit("stage_done", "write_viewer", None, 1, 1, 110.8, seconds=108.3, done=21193, unit="pages")
emit("stage_start", "check", None, 0, 1, 110.8)
emit("file_start", "check", "Synapse", 1, 1, 110.8)
emit("file_done", "check", "Synapse", 1, 1, 145.4, seconds=34.6)
emit("stage_done", "check", None, 1, 1, 145.4, seconds=34.6, done=900, unit="files")
emit("done", None, None, 0, 0, 147.0)

out = os.path.join(docs_root, solution)
os.makedirs(out, exist_ok=True)
with open(os.path.join(out, "docs.json"), "w", encoding="utf-8") as f:
    json.dump({"solution": solution, "kind": "batch", "files": []}, f)
'''


@pytest.fixture
def fake_units(tmp_path, monkeypatch):
    """A fake fm-saxml with unit stages; it waits mid write_viewer until the returned gate file exists."""
    script = tmp_path / "fake_units.py"
    script.write_text(FAKE_UNITS, encoding="utf-8")
    gate = tmp_path / "gate_units"
    monkeypatch.setattr(cs, "_convert_command", lambda export_path, docs_root, solution_name, **kwargs:
                        [sys.executable, str(script), docs_root, solution_name, str(gate)])
    return gate


def _job_with(events: list) -> dict:
    """Record events into a fresh job and return its progress block."""
    with cs._convert_lock:
        cs._convert_jobs["unitjob"] = {"job_id": "unitjob", "done": False, "progress": {}}
    for event in events:
        cs._record_progress("unitjob", event)
    return cs._get_convert_job("unitjob")["progress"]


def _ev(event, stage, elapsed, file=None, index=1, total=1, **extra):
    return {"event": event, "stage": stage, "file": file, "index": index, "total": total,
            "elapsed": elapsed, **extra}


def test_stage_progress_updates_the_block():
    progress = _job_with([
        _ev("stage_start", "write_viewer", 16.0, index=0),
        _ev("file_start", "write_viewer", 16.0, file="Synapse"),
        _ev("stage_progress", "write_viewer", 61.0, file="Synapse", done=5300, units=21193, unit="pages"),
    ])
    assert progress == {"stage": "write_viewer", "file": "Synapse", "index": 1, "total": 1, "elapsed": 61.0,
                        "done": 5300, "units": 21193, "unit": "pages", "stage_elapsed": 45.0, "files": []}


def test_stage_start_removes_the_unit_fields():
    events = [_ev("stage_start", "write_pages", 1.0, index=0),
              _ev("stage_progress", "write_pages", 3.0, file="A", done=4, units=8, unit="pages"),
              _ev("stage_done", "write_pages", 4.0, seconds=3.0, done=9, unit="pages")]
    progress = _job_with(events)
    assert (progress["done"], progress["units"], progress["unit"], progress["stage_elapsed"]) == (9, 8, "pages", 3.0)
    progress = _job_with(events + [_ev("stage_start", "write_raw", 4.0, index=0)])
    assert not set(cs.CONVERT_UNIT_KEYS) & set(progress)
    progress = _job_with(events + [_ev("done", None, 5.0, index=0, total=0)])
    assert not set(cs.CONVERT_UNIT_KEYS) & set(progress)


def test_stage_done_without_a_stage_progress_keeps_its_count():
    progress = _job_with([_ev("stage_start", "check", 10.0, index=0),
                          _ev("stage_done", "check", 44.6, seconds=34.6, done=900, unit="files")])
    assert (progress["done"], progress["units"], progress["unit"], progress["stage_elapsed"]) == \
           (900, 900, "files", 34.6)


# Real lines from fm-saxml (a 3-file --html --webmcp scrubbed build, file name replaced), as sent by saxml-stages.
REAL_UNIT_LINES = """\
{"event": "stage_start", "stage": "write_viewer", "file": null, "index": 0, "total": 1, "elapsed": 38.4}
{"event": "file_start", "stage": "write_viewer", "file": "Solution", "index": 1, "total": 1, "elapsed": 38.4}
{"event": "stage_progress", "stage": "write_viewer", "file": "Solution", "index": 1, "total": 1, "elapsed": 39.4, "done": 150, "units": 21193, "unit": "pages"}
{"event": "stage_done", "stage": "write_viewer", "file": null, "index": 1, "total": 1, "elapsed": 150.2, "seconds": 111.8, "done": 21193, "unit": "pages"}
{"event": "stage_start", "stage": "check", "file": null, "index": 0, "total": 1, "elapsed": 151.7}
{"event": "stage_progress", "stage": "check", "file": "Solution", "index": 1, "total": 1, "elapsed": 152.7, "done": 305, "units": 42481, "unit": "files"}
{"event": "stage_done", "stage": "check", "file": null, "index": 1, "total": 1, "elapsed": 184.0, "seconds": 32.4, "done": 42481, "unit": "files"}
"""


def test_real_fm_saxml_unit_lines():
    events = [cs._parse_progress_line(line + "\n") for line in REAL_UNIT_LINES.splitlines()]
    assert all(events)
    progress = _job_with(events[:3])
    assert {k: progress[k] for k in cs.CONVERT_UNIT_KEYS} == \
           {"done": 150, "units": 21193, "unit": "pages", "stage_elapsed": 1.0}
    progress = _job_with(events[:4])
    assert {k: progress[k] for k in cs.CONVERT_UNIT_KEYS} == \
           {"done": 21193, "units": 21193, "unit": "pages", "stage_elapsed": 111.8}
    assert cs._progress_message(events[3]) == "Writing HTML viewer done: 21193 pages (111.8s)"
    progress = _job_with(events[:5])
    assert progress["stage"] == "check" and not set(cs.CONVERT_UNIT_KEYS) & set(progress)
    progress = _job_with(events)
    assert {k: progress[k] for k in cs.CONVERT_UNIT_KEYS} == \
           {"done": 42481, "units": 42481, "unit": "files", "stage_elapsed": 32.3}
    assert cs._progress_message(events[6]) == "Checking redactions done: 42481 files (32.4s)"


def test_check_with_nothing_to_scan_has_zero_units():
    # Nothing redacted: check runs with no stage_progress and a stage_done of 0.
    progress = _job_with([_ev("stage_start", "check", 151.7, index=0),
                          _ev("stage_done", "check", 151.8, seconds=0.1, done=0, unit="files")])
    assert (progress["done"], progress["units"], progress["unit"]) == (0, 0, "files")


@pytest.mark.parametrize("event, message", [
    (_ev("stage_progress", "write_viewer", 61.3, file="Synapse", done=5300, units=21193, unit="pages"),
     "Writing HTML viewer: 5300/21193 pages"),
    (_ev("stage_done", "write_viewer", 124.3, seconds=108.3, done=21193, unit="pages"),
     "Writing HTML viewer done: 21193 pages (108.3s)"),
    (_ev("stage_done", "check", 160.0, seconds=34.6, done=900, unit="files"),
     "Checking redactions done: 900 files (34.6s)"),
    (_ev("stage_start", "check", 125.0, index=0), "Checking redactions: 1 file(s)"),
])
def test_unit_stage_messages(event, message):
    assert cs._progress_message(event) == message


def test_unit_stages_end_to_end(server, export, tmp_path, fake_units, caplog):
    with caplog.at_level(logging.DEBUG, logger="companion_server"):
        job_id = _start(server, export, tmp_path)
        job = _wait(server, job_id, until=lambda j: j["progress"].get("done") == 5300)
        assert {k: job["progress"][k] for k in cs.CONVERT_UNIT_KEYS} == \
               {"done": 5300, "units": 21193, "unit": "pages", "stage_elapsed": 45.0}
        fake_units.write_text("go", encoding="utf-8")
        job = _wait(server, job_id)
    assert job["state"] == "succeeded", job
    assert not set(cs.CONVERT_UNIT_KEYS) & set(job["progress"])
    info = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert f"convert {job_id}: Writing HTML viewer done: 21193 pages (108.3s)" in info
    assert f"convert {job_id}: Checking redactions done: 900 files (34.6s)" in info
    assert not any("/21193 pages" in m or "10/20 pages" in m for m in info)    # stage_progress: debug only
    assert f"convert {job_id}: Writing HTML viewer: 5300/21193 pages" in debug


# -- one line per file while the live panel is on ------------------------------------

@pytest.mark.parametrize("panel_on", [False, True])
def test_convert_file_start_lines_follow_the_panel(server, export, tmp_path, fake, caplog, monkeypatch, panel_on):
    monkeypatch.setattr(cs, "_live_panel_on", panel_on)
    with caplog.at_level(logging.DEBUG, logger="companion_server"):
        job_id = _start(server, export, tmp_path)
        assert _wait(server, job_id)["state"] == "succeeded"
    levels = {r.getMessage(): r.levelno for r in caplog.records}
    start_level = logging.DEBUG if panel_on else logging.INFO
    assert levels[f"convert {job_id}: Analyzing 1/2 FileA"] == start_level
    assert levels[f"convert {job_id}: Analyzing 2/2 FileB"] == start_level
    for line in ("Analyzing: 2 file(s)", "Analyzing 1/2 FileA (3.1s)", "Analyzing done (3.6s)",
                 "fm-saxml done (3.8s)"):
        assert levels[f"convert {job_id}: {line}"] == logging.INFO


def test_start_live_panel_sets_the_switch_and_stop_clears_it(monkeypatch):
    import companion_live

    class View:
        stopped = False

        def stop(self):
            View.stopped = True

    monkeypatch.setattr(cs, "_live_panel_on", False)
    monkeypatch.setattr(companion_live, "start_live", lambda *a, **k: (View(), "Live progress panel: on"))
    live = cs._start_live_panel(False)
    assert cs._live_panel_on is True
    cs._stop_live_panel(live)
    assert cs._live_panel_on is False and View.stopped

    monkeypatch.setattr(companion_live, "start_live", lambda *a, **k: (None, "Live progress panel: off"))
    assert cs._start_live_panel(False) is None and cs._live_panel_on is False
    cs._stop_live_panel(None)
