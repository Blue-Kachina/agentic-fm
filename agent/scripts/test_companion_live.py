"""test_companion_live.py - the companion's live progress panel and its plain fallback (roadmap P3-14).

The panel itself needs Rich (the harness .venv has it); the fallback tests block Rich to check
that the companion still imports, starts and prints plain lines without it.
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

import companion_live as cl

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(SCRIPTS, "companion_server.py")
ANSI = "\x1b"


class _Tty(io.StringIO):
    def isatty(self):
        return True


# ---------------------------------------------------------------------------
# Choosing live or plain
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("is_tty, has_rich, disabled, expected", [
    (True, True, False, "live"),
    (False, True, False, "plain"),
    (True, False, False, "plain"),
    (False, False, False, "plain"),
    (True, True, True, "plain"),
    (False, True, True, "plain"),
    (True, False, True, "plain"),
    (False, False, True, "plain"),
])
def test_choose_mode(is_tty, has_rich, disabled, expected):
    assert cl.choose_mode(is_tty, has_rich, disabled) == expected


@pytest.mark.parametrize("flag, value, expected", [
    (False, None, False), (False, "", False), (False, "0", False), (False, "false", False),
    (False, "1", True), (False, "yes", True), (True, None, True), (True, "0", True),
])
def test_opt_out(flag, value, expected):
    environ = {} if value is None else {cl.NO_LIVE_ENV: value}
    assert cl.live_disabled(flag, environ) is expected


def test_is_terminal():
    assert cl.is_terminal(io.StringIO(), {}) is False
    assert cl.is_terminal(_Tty(), {}) is True
    assert cl.is_terminal(_Tty(), {"TERM": "dumb"}) is False
    assert cl.is_terminal(object(), {}) is False


def test_not_a_terminal_starts_plain(monkeypatch):
    monkeypatch.delenv(cl.NO_LIVE_ENV, raising=False)
    handlers = logging.getLogger().handlers[:]
    view, message = cl.start_live(dict, list, stream=io.StringIO())
    assert view is None and "not a terminal" in message
    assert logging.getLogger().handlers == handlers


def test_opt_out_starts_plain(monkeypatch):
    monkeypatch.setenv(cl.NO_LIVE_ENV, "1")
    view, message = cl.start_live(dict, list, stream=_Tty())
    assert view is None and "turned off" in message
    monkeypatch.delenv(cl.NO_LIVE_ENV)
    view, message = cl.start_live(dict, list, no_live=True, stream=_Tty())
    assert view is None and "turned off" in message


def test_without_rich_the_module_imports_and_is_plain(monkeypatch):
    for name in ("rich", "rich.live", "rich.logging"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.delitem(sys.modules, "companion_live")
    monkeypatch.delenv(cl.NO_LIVE_ENV, raising=False)
    fresh = importlib.import_module("companion_live")
    assert fresh.rich_available() is False
    view, message = fresh.start_live(dict, list, stream=_Tty())
    assert view is None and "Rich is not installed" in message


# ---------------------------------------------------------------------------
# Panel content
# ---------------------------------------------------------------------------

NOW = 1000.0
UTC_NOW = datetime(2026, 10, 29, 14, 10, 0, tzinfo=timezone.utc)


def _export(**extra):
    state = {"solution_name": "Invoices", "stage": "export", "event": "file_start", "file": "Invoices.fmp12",
             "index": 3, "total": 12, "started_at": NOW - 25.0, "updated_at": NOW - 1.0,
             "files": [{"file": "Contacts.fmp12", "seconds": 2.0}, {"file": "Lines.fmp12", "seconds": 4.0}],
             "done": False}
    state.update(extra)
    return state


def _job(**extra):
    job = {"job_id": "3f9c0a1b2c4d", "state": "running", "done": False, "solution_name": "Sales",
           "started_at": (UTC_NOW - timedelta(seconds=75)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "progress": {"stage": "analyze", "file": "Invoices", "index": 2, "total": 5, "elapsed": 70.0,
                        "files": [{"stage": "read", "file": "Contacts", "seconds": 9.0},
                                  {"stage": "analyze", "file": "Contacts", "seconds": 6.0}]}}
    job.update(extra)
    return job


def _rows(exports, jobs):
    return cl.build_rows(exports, jobs, now=NOW, utc_now=UTC_NOW, stage_labels=cl.DEFAULT_STAGE_LABELS)


def _text(rows) -> str:
    from rich.console import Console
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=100).print(cl.render_panel(rows))
    return out.getvalue()


def test_rows_and_estimates():
    export, convert = _rows({"invoices": _export()}, [_job()])
    assert (export["step"], export["index"], export["total"]) == ("export", 3, 12)
    assert export["elapsed"] == 25.0
    assert export["left"] == pytest.approx(3.0 * 10)          # average 3.0s × 10 files not finished
    assert (convert["step"], convert["stage"], convert["index"], convert["total"]) == ("convert", "Analyzing", 2, 5)
    assert convert["finished"] == [("Contacts", 6.0)]           # only the current stage's files
    assert convert["elapsed"] == 75.0
    assert convert["left"] == pytest.approx(6.0 * 4)


def test_finished_and_stale_runs_leave_the_panel():
    exports = {"a": _export(done=True), "b": _export(updated_at=NOW - cl.EXPORT_STALE_SECONDS - 1)}
    assert _rows(exports, [_job(done=True, state="succeeded")]) == []
    assert _text([]).strip() == ""


def test_panel_text():
    text = _text(_rows([_export()], [_job()]))
    assert ANSI not in text
    assert "export" in text and "Invoices" in text and "file 3 of 12" in text and "Invoices.fmp12" in text
    assert "25.0s elapsed" in text and "about 30.0s left" in text
    assert "finished: Contacts.fmp12 2.0s, Lines.fmp12 4.0s" in text
    assert "convert" in text and "Sales" in text and "Analyzing: file 2 of 5" in text
    assert "1m 15s elapsed" in text and "about 24.0s left in this stage" in text


def test_panel_before_the_first_file_and_while_queued():
    text = _text(_rows([_export(index=1, files=[])],
                       [_job(state="queued", started_at="", progress={})]))
    assert "file 1 of 12" in text and "time left: estimating" in text
    assert "queued" in text


def test_convert_after_fm_saxmls_last_event_is_finishing():
    progress = {"stage": None, "file": None, "index": 0, "total": 0, "elapsed": 80.0,
                "files": [{"stage": "write_pages", "file": "Contacts", "seconds": 1.0}]}
    text = _text(_rows([], [_job(progress=progress)]))
    assert "Finishing:" in text and "1m 15s elapsed" in text and "left" not in text


def _units_job(done, stage_elapsed=45.0):
    return _job(progress={"stage": "write_viewer", "file": "Synapse", "index": 1, "total": 1, "elapsed": 61.3,
                          "done": done, "units": 21193, "unit": "pages", "stage_elapsed": stage_elapsed,
                          "files": [{"stage": "write_pages", "file": "Synapse", "seconds": 14.0}]})


def test_stage_with_units_counts_and_estimates_by_unit():
    (row,) = _rows([], [_units_job(5300)])
    assert (row["done_units"], row["units"], row["unit"]) == (5300, 21193, "pages")
    assert row["left"] == pytest.approx(45.0 / 5300 * (21193 - 5300))
    text = _text([row])
    assert "Writing HTML viewer: 5,300 of 21,193 pages, file 1 of 1 Synapse" in text
    assert "about 2m 15s left in this stage" in text


def test_stage_with_units_before_the_first_unit_is_estimating():
    text = _text(_rows([], [_units_job(0, stage_elapsed=0.0)]))
    assert "0 of 21,193 pages" in text and "time left: estimating" in text


def test_stage_without_units_keeps_the_per_file_estimate():
    (row,) = _rows([], [_job()])
    assert "units" not in row and row["left"] == pytest.approx(6.0 * 4)
    assert " of " not in _text([row]).split("Analyzing:")[1].split("file 2 of 5")[0]


def test_format_seconds():
    assert [cl.format_seconds(s) for s in (None, 8.44, 65, 3725)] == ["?", "8.4s", "1m 05s", "1h 02m"]


def test_live_view_routes_logging_through_rich_and_restores_it():
    from rich.console import Console
    from rich.logging import RichHandler

    root = logging.getLogger()
    before = root.handlers[:]
    out = io.StringIO()
    view = cl.LiveView(lambda: {"invoices": _export(updated_at=time.monotonic())}, list,
                       console=Console(file=out, force_terminal=True, width=100), refresh_per_second=20)
    view.start()
    try:
        # The panel's handler replaces the console handlers; file handlers (pytest's own here) stay
        assert type(root.handlers[0]) is RichHandler
        assert not [h for h in root.handlers[1:] if cl.is_console_handler(h)]
        logging.getLogger("companion_server").warning("a log line above the panel")
        time.sleep(0.2)
    finally:
        view.stop()
    assert root.handlers == before
    printed = out.getvalue()
    assert "a log line above the panel" in printed and "file 3 of 12" in printed


def test_live_view_keeps_the_log_file_and_holds_the_console_at_info(tmp_path):
    from rich.console import Console

    root, logger = logging.getLogger(), logging.getLogger("companion_server")
    path = tmp_path / "companion-server.log"
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)
    out = io.StringIO()
    view = cl.LiveView(lambda: {}, list, console=Console(file=out, force_terminal=True, width=100),
                       refresh_per_second=20)
    try:
        view.start()
        try:
            assert file_handler in root.handlers
            logger.info("an info line while the panel is drawn")
            logger.debug("a debug line while the panel is drawn")
            time.sleep(0.1)
        finally:
            view.stop()
        assert file_handler in root.handlers
    finally:
        root.removeHandler(file_handler)
        file_handler.close()
        logger.setLevel(old_level)
    written = path.read_text(encoding="utf-8")
    assert "an info line while" in written and "a debug line while" in written
    printed = out.getvalue()
    assert "an info line while" in printed and "a debug line while" not in printed


def test_is_console_handler():
    assert cl.is_console_handler(logging.StreamHandler(io.StringIO()))
    assert not cl.is_console_handler(logging.NullHandler())


def test_renderable_survives_a_bad_state():
    view = cl.LiveView(lambda: {"x": {"started_at": "not a number", "done": False}}, list)
    assert "progress panel error" in _text_of(view.renderable())


def _text_of(renderable) -> str:
    from rich.console import Console
    out = io.StringIO()
    Console(file=out, force_terminal=False, width=100).print(renderable)
    return out.getvalue()


# ---------------------------------------------------------------------------
# The companion's own output when not a terminal (subprocess)
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run_companion(prelude: str, port: int, events: list) -> str:
    """Start the companion with stdout piped, wait for it, POST events, stop it; return its output."""
    code = (f"import runpy, sys\n{prelude}\nsys.argv = ['companion_server.py', '--port', '{port}']\n"
            f"runpy.run_path({SERVER!r}, run_name='__main__')\n")
    # Its log file and state file go to a test folder, never the real per-user one
    state_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
    env = {**os.environ, "COMPANION_IDLE_TIMEOUT": "0", "COMPANION_STATE_DIR": state_dir.name}
    env.pop(cl.NO_LIVE_ENV, None)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, cwd=SCRIPTS)
    lines: list = []
    reader = threading.Thread(target=lambda: lines.extend(iter(proc.stdout.readline, b"")), daemon=True)
    reader.start()

    def wait_for(needle: str) -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if needle in b"".join(lines).decode("utf-8", "replace"):
                return
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        raise AssertionError(f"{needle!r} not in output:\n" + b"".join(lines).decode("utf-8", "replace"))

    try:
        wait_for("Press Ctrl-C")
        for event in events:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/progress", method="POST",
                                         data=json.dumps(event).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                assert resp.status == 200
        if events:
            wait_for("Export done")
    finally:
        proc.terminate()
        proc.wait(10)
        reader.join(5)
        state_dir.cleanup()
    return b"".join(lines).decode("utf-8", "replace")


EVENTS = [
    {"solution_name": "Invoices", "stage": "export", "event": "file_start", "file": "A.fmp12", "index": 1, "total": 2},
    {"solution_name": "Invoices", "stage": "export", "event": "file_done", "file": "A.fmp12", "index": 1, "total": 2},
    {"solution_name": "Invoices", "stage": "export", "event": "file_start", "file": "B.fmp12", "index": "2", "total": "2"},
    {"solution_name": "Invoices", "stage": "export", "event": "file_done", "file": "B.fmp12", "index": "2", "total": "2"},
    {"solution_name": "Invoices", "stage": "export", "event": "done", "file": "", "index": 2, "total": 2},
]


def test_piped_output_is_plain():
    output = _run_companion("", _free_port(), EVENTS)
    assert ANSI not in output
    assert "Live progress panel: off, stdout is not a terminal" in output
    assert "export Invoices: Exporting 1/2 A.fmp12" in output
    assert "export Invoices: Exported 2/2 B.fmp12 (" in output
    assert "export Invoices: Export done: 2 file(s) in" in output


def test_companion_starts_without_rich():
    output = _run_companion("for name in ('rich', 'rich.live', 'rich.logging'):\n    sys.modules[name] = None",
                            _free_port(), EVENTS[:1] + EVENTS[-1:])
    assert ANSI not in output
    assert "listening on" in output and "Live progress panel: off" in output
    assert "export Invoices: Exporting 1/2 A.fmp12" in output
