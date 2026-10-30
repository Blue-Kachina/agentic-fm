"""test_companion_control.py - status, stop, attach and the port clash check (P3-20, P3-21).

A small fake companion (http.server on 127.0.0.1:0) answers /health and /shutdown;
the state file, PID checks and port owner are faked on companion_state, so nothing
touches the real per-user state folder or any real process.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import companion_control as cc
import companion_state


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        fake = self.server.fake
        if self.path == "/health" and fake.health is not None:
            self._send(200, fake.health)
        else:
            self._send(404, {"error": "not here"})

    def do_POST(self):
        fake = self.server.fake
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        fake.shutdown_calls.append({"token": self.headers.get(companion_state.TOKEN_HEADER), "body": body})
        status, reply = fake.shutdown_reply
        self._send(status, reply)
        if status == 202 and fake.stops:
            fake.health = None
            fake.processes.alive.discard(fake.pid)


class FakeProcesses:
    """companion_state's state file, PID checks and port owner, in memory."""

    def __init__(self, tmp_path):
        self.state = None
        self.alive = set()
        self.companions = set()
        self.owner = None
        self.terminated = []
        self.removed = []
        self.path = str(tmp_path / "companion.state.json")

    def check_state(self, state=None):
        state = self.state if state is None else state
        if not state:
            return {"state": None, "running": False, "stale": False, "reason": "no state file"}
        pid = state.get("pid")
        if pid not in self.alive:
            return {"state": state, "running": False, "stale": True, "reason": "process gone"}
        if pid not in self.companions:
            return {"state": state, "running": False, "stale": True, "reason": "pid reused by another program"}
        return {"state": state, "running": True, "stale": False, "reason": "running"}

    def remove_state(self, path=None, pid=None):
        if self.state and (pid is None or self.state.get("pid") == pid):
            self.removed.append(pid)
            self.state = None
            return True
        return False

    def terminate_process(self, pid, timeout=5.0):
        self.terminated.append(pid)
        self.alive.discard(pid)
        self.companions.discard(pid)
        return "terminated"


@pytest.fixture(autouse=True)
def quick_health(monkeypatch):
    """Windows takes about 2s to refuse a connection to a closed port: don't wait that long."""
    fetch = cc.fetch_health
    monkeypatch.setattr(cc, "fetch_health", lambda host, port, timeout=2.0: fetch(host, port, min(timeout, 0.3)))


@pytest.fixture
def procs(tmp_path, monkeypatch):
    fake = FakeProcesses(tmp_path)
    monkeypatch.setenv(companion_state.STATE_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(companion_state, "read_state", lambda path=None: fake.state)
    monkeypatch.setattr(companion_state, "check_state", fake.check_state)
    monkeypatch.setattr(companion_state, "remove_state", fake.remove_state)
    monkeypatch.setattr(companion_state, "state_file_path", lambda: fake.path)
    monkeypatch.setattr(companion_state, "process_alive", lambda pid: pid in fake.alive)
    monkeypatch.setattr(companion_state, "is_companion_process",
                        lambda pid: pid in fake.alive and pid in fake.companions)
    monkeypatch.setattr(companion_state, "terminate_process", fake.terminate_process)
    monkeypatch.setattr(companion_state, "port_owner", lambda port: fake.owner)
    return fake


class FakeCompanion:
    def __init__(self, procs, pid=4321):
        self.processes = procs
        self.pid = pid
        self.health = None
        self.shutdown_calls = []
        self.shutdown_reply = (202, {"status": "stopping"})
        self.stops = True
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.fake = self
        self.port = self.httpd.server_address[1]

    def running(self, **fields):
        """Make it a running companion of ours, with a matching state file."""
        self.health = {"status": "ok", "version": "1.2.3", "plugin": {}, "pid": self.pid,
                       "started_at": "2026-10-30T10:00:00Z", "uptime_seconds": 3725.0,
                       "host": "127.0.0.1", "port": self.port, "afm_root": cc.AFM_ROOT,
                       "python": "python.exe", "log_file": None, "launched_by": "background",
                       "conversion": None}
        self.health.update(fields)
        self.processes.state = {k: v for k, v in self.health.items()
                                if k not in ("plugin", "uptime_seconds", "conversion", "status")}
        self.processes.state["token"] = "secret-token"
        self.processes.alive.add(self.pid)
        self.processes.companions.add(self.pid)
        return self


@pytest.fixture
def companion(procs):
    fake = FakeCompanion(procs)
    thread = threading.Thread(target=fake.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    yield fake
    fake.httpd.shutdown()
    fake.httpd.server_close()


@pytest.fixture
def free_port():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    httpd.server_close()
    return port


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def test_fetch_health_returns_a_companion(companion):
    companion.running()
    health = cc.fetch_health("127.0.0.1", companion.port)
    assert health["pid"] == companion.pid


def test_fetch_health_ignores_what_is_not_a_companion(companion, free_port):
    assert cc.fetch_health("127.0.0.1", companion.port) is None          # 404
    companion.health = {"status": "fine"}
    assert cc.fetch_health("127.0.0.1", companion.port) is None          # not a companion's body
    assert cc.fetch_health("127.0.0.1", free_port, timeout=0.5) is None  # nothing listening


def test_request_shutdown_sends_the_token_and_flags(companion):
    companion.running()
    status, body = cc.request_shutdown("127.0.0.1", companion.port, "tok", force=True, wait=True)
    assert (status, body) == (202, {"status": "stopping"})
    assert companion.shutdown_calls == [{"token": "tok", "body": {"force": True, "wait": True}}]


def test_request_shutdown_returns_the_error_body(companion):
    companion.running()
    companion.shutdown_reply = (403, {"error": "bad token"})
    assert cc.request_shutdown("127.0.0.1", companion.port, "nope") == (403, {"error": "bad token"})


def test_describe_names_pid_start_folder_log_and_conversion():
    text = cc.describe({"status": "ok", "version": "1", "pid": 7, "started_at": "2026-10-30T10:00:00Z",
                        "uptime_seconds": 65, "afm_root": "C:/afm", "log_file": "C:/x.log",
                        "launched_by": "logon task",
                        "conversion": {"job_id": "abc", "state": "running", "solution_name": "Invoices"}},
                       None)
    for part in ("7", "2026-10-30T10:00:00Z (up 1m 05s)", "C:/afm", "C:/x.log", "logon task",
                 "Invoices (job abc, running)"):
        assert part in text


def test_describe_lists_served_viewers():
    text = cc.describe({"status": "ok", "viewers": [
        {"output_path": "C:/docs/Invoices", "url": "http://127.0.0.1:8766/index.html"}]}, None)
    assert "Viewer      http://127.0.0.1:8766/index.html (C:/docs/Invoices)" in text
    assert "Viewer" not in cc.describe({"status": "ok", "viewers": []}, None)


# ---------------------------------------------------------------------------
# status (P3-20 AC3)
# ---------------------------------------------------------------------------

def test_status_running(companion):
    companion.running()
    out = io.StringIO()
    assert cc.cmd_status("127.0.0.1", companion.port, out=out) == 0
    assert "A companion is running" in out.getvalue()
    assert str(companion.pid) in out.getvalue()


def test_status_not_running_mentions_a_stale_state_file(procs, free_port):
    procs.state = {"pid": 999, "port": free_port, "token": "t"}
    out = io.StringIO()
    assert cc.cmd_status("127.0.0.1", free_port, out=out) == 1
    assert "No companion running" in out.getvalue()
    assert "stale (process gone" in out.getvalue()


def test_status_companion_that_does_not_answer(procs, free_port):
    procs.state = {"pid": 55, "port": free_port, "token": "t"}
    procs.alive.add(55)
    procs.companions.add(55)
    out = io.StringIO()
    assert cc.cmd_status("127.0.0.1", free_port, out=out) == 0
    assert "doesn't answer" in out.getvalue()


# ---------------------------------------------------------------------------
# stop (P3-20 AC4, AC7)
# ---------------------------------------------------------------------------

def test_stop_sends_the_state_token_and_waits(companion, procs):
    companion.running()
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", companion.port, timeout=2, out=out) == 0
    assert companion.shutdown_calls == [{"token": "secret-token", "body": {"force": False, "wait": False}}]
    assert procs.terminated == []
    assert "Stopped." in out.getvalue()


def test_stop_refuses_while_converting(companion, procs):
    job = {"job_id": "j1", "state": "running", "solution_name": "Invoices"}
    companion.running(conversion=job)
    companion.shutdown_reply = (409, {"error": "conversion running", "job": job})
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", companion.port, timeout=1, out=out) == 2
    assert "Invoices (job j1, running)" in out.getvalue()
    assert "--force" in out.getvalue()
    assert procs.terminated == []


def test_stop_force_passes_force(companion):
    companion.running(conversion={"job_id": "j1", "state": "running", "solution_name": "Invoices"})
    assert cc.cmd_stop("127.0.0.1", companion.port, timeout=2, force=True, out=io.StringIO()) == 0
    assert companion.shutdown_calls[0]["body"]["force"] is True


def test_stop_terminates_a_companion_that_does_not_go(companion, procs, monkeypatch):
    companion.running()
    companion.stops = False          # says 202 but keeps running
    out = io.StringIO()

    def terminate(pid, timeout=5.0):
        companion.health = None
        return procs.terminate_process(pid, timeout)

    monkeypatch.setattr(companion_state, "terminate_process", terminate)
    assert cc.cmd_stop("127.0.0.1", companion.port, timeout=0.3, out=out) == 0
    assert procs.terminated == [companion.pid]
    assert "Still running after 0.3s" in out.getvalue()


def test_stop_never_kills_a_pid_that_is_no_longer_a_companion(companion, procs):
    companion.running()
    companion.stops = False
    procs.companions.discard(companion.pid)      # PID now belongs to another program
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", companion.port, timeout=0.2, out=out) == 1
    assert procs.terminated == []
    assert "no longer a companion" in out.getvalue()


def test_stop_terminates_a_companion_that_does_not_answer_http(procs, free_port):
    procs.state = {"pid": 77, "port": free_port, "token": "t"}
    procs.alive.add(77)
    procs.companions.add(77)
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", free_port, timeout=1, out=out) == 0
    assert procs.terminated == [77]
    assert procs.state is None                   # its state file is gone too
    assert "doesn't answer" in out.getvalue()


def test_stop_removes_a_stale_state_file(procs, free_port):
    procs.state = {"pid": 88, "port": free_port, "token": "t"}     # process gone
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", free_port, out=out) == 0
    assert procs.removed == [88]
    assert "Removed the stale state file" in out.getvalue()
    assert "No companion running" in out.getvalue()


def test_stop_with_nothing_running(procs, free_port):
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", free_port, out=out) == 0
    assert "No companion running" in out.getvalue()


def test_stop_without_a_token_does_not_kill_mid_conversion(companion, procs):
    companion.running(conversion={"job_id": "j2", "state": "queued", "solution_name": "CRM"})
    procs.state = None                           # no state file: no token
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", companion.port, timeout=0.2, out=out) == 2
    assert companion.shutdown_calls == []
    assert procs.terminated == []


# ---------------------------------------------------------------------------
# follow_log (P3-21 T2)
# ---------------------------------------------------------------------------

class _Out(io.StringIO):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()

    def write(self, text):
        with self.lock:
            return super().write(text)


@pytest.fixture
def follower(tmp_path):
    started = []

    def start(path, lines=3):
        out = _Out()
        stop = threading.Event()
        thread = threading.Thread(target=cc.follow_log, args=(str(path), out, stop),
                                  kwargs={"lines": lines, "poll": 0.02}, daemon=True)
        thread.start()
        started.append((stop, thread))
        return out

    yield start
    for stop, thread in started:
        stop.set()
        thread.join(1)


def _append(path, text):
    with open(path, "a", encoding="utf-8", newline="") as fh:
        fh.write(text)


def test_follow_log_tails_then_follows(tmp_path, follower):
    log_file = tmp_path / "companion-server.log"
    log_file.write_text("".join("line %d\n" % i for i in range(10)), encoding="utf-8")
    out = follower(log_file, lines=3)
    assert _wait_for(lambda: "line 9" in out.getvalue())
    assert "line 6" not in out.getvalue() and "line 7\nline 8\nline 9\n" in out.getvalue()
    _append(log_file, "new one\npartial")
    assert _wait_for(lambda: "new one\n" in out.getvalue())
    assert "partial" not in out.getvalue()
    _append(log_file, " line\n")
    assert _wait_for(lambda: "partial line\n" in out.getvalue())


def test_follow_log_survives_rotation(tmp_path, follower):
    log_file = tmp_path / "companion-server.log"
    log_file.write_text("first\n", encoding="utf-8")
    out = follower(log_file)
    assert _wait_for(lambda: "first" in out.getvalue())
    # What RotatingFileHandler does: rename to .1, start a new file. The follower must not hold
    # the file open, or the rename fails on Windows.
    _append(log_file, "last before rotation\n")
    os.replace(log_file, str(log_file) + ".1")
    log_file.write_text("after rotation\n", encoding="utf-8")
    assert _wait_for(lambda: "after rotation" in out.getvalue())
    text = out.getvalue()
    assert text.count("first") == 1
    assert "last before rotation" in text
    assert text.index("last before rotation") < text.index("after rotation")


def test_follow_log_rereads_a_truncated_file(tmp_path, follower):
    log_file = tmp_path / "companion-server.log"
    log_file.write_text("a fairly long first line\n", encoding="utf-8")
    out = follower(log_file)
    assert _wait_for(lambda: "first line" in out.getvalue())
    log_file.write_text("short\n", encoding="utf-8")
    assert _wait_for(lambda: "short" in out.getvalue())


def test_follow_log_waits_for_a_missing_file(tmp_path, follower):
    log_file = tmp_path / "companion-server.log"
    out = follower(log_file)
    assert _wait_for(lambda: "waiting for" in out.getvalue())
    log_file.write_text("hello\n", encoding="utf-8")
    assert _wait_for(lambda: "hello" in out.getvalue())


# ---------------------------------------------------------------------------
# attach (P3-21 AC2, AC3)
# ---------------------------------------------------------------------------

def _ctrl_c_once(monkeypatch):
    calls = []

    def wait(stop_event, seconds):
        calls.append(seconds)
        if len(calls) == 1:
            time.sleep(0.1)          # let the follower print the tail
            raise KeyboardInterrupt
        return stop_event.wait(seconds)

    monkeypatch.setattr(cc, "_attach_wait", wait)


@pytest.fixture
def attached(companion, tmp_path):
    log_file = tmp_path / "companion-server.log"
    log_file.write_text("hello from the companion\n", encoding="utf-8")
    companion.running(log_file=str(log_file))
    return companion


def test_attach_ctrl_c_then_detach(attached, monkeypatch, procs):
    _ctrl_c_once(monkeypatch)
    out = io.StringIO()
    assert cc.cmd_attach("127.0.0.1", attached.port, out=out, stdin=io.StringIO("d\n")) == 0
    text = out.getvalue()
    assert "hello from the companion" in text
    assert "[d]etach" in text and "Detached" in text
    assert attached.shutdown_calls == [] and procs.terminated == []


def test_attach_ctrl_c_then_stop(attached, monkeypatch):
    _ctrl_c_once(monkeypatch)
    stops = []
    monkeypatch.setattr(cc, "cmd_stop", lambda host, port, **kw: stops.append(port) or 0)
    assert cc.cmd_attach("127.0.0.1", attached.port, out=io.StringIO(), stdin=io.StringIO("s\n")) == 0
    assert stops == [attached.port]


def test_attach_second_ctrl_c_detaches(attached, monkeypatch):
    _ctrl_c_once(monkeypatch)

    class CtrlC:
        def readline(self):
            raise KeyboardInterrupt

    out = io.StringIO()
    assert cc.cmd_attach("127.0.0.1", attached.port, out=out, stdin=CtrlC()) == 0
    assert "Detached" in out.getvalue()
    assert attached.shutdown_calls == []


def test_attach_ends_when_the_companion_stops(attached, monkeypatch):
    monkeypatch.setattr(cc, "ATTACH_HEALTH_EVERY", 0.05)
    threading.Timer(0.2, lambda: setattr(attached, "health", None)).start()
    out = io.StringIO()
    assert cc.cmd_attach("127.0.0.1", attached.port, out=out, stdin=io.StringIO()) == 0
    assert "The companion stopped." in out.getvalue()


def test_attach_without_a_log_file(companion):
    companion.running(log_file=None)
    out = io.StringIO()
    assert cc.cmd_attach("127.0.0.1", companion.port, out=out) == 1
    assert "no log file" in out.getvalue()


def test_attach_with_nothing_running(procs, free_port):
    out = io.StringIO()
    assert cc.cmd_attach("127.0.0.1", free_port, out=out) == 1
    assert "No companion running" in out.getvalue()


# ---------------------------------------------------------------------------
# Port clash at start (P3-21)
# ---------------------------------------------------------------------------

def test_clash_with_our_companion_attaches(companion, monkeypatch):
    companion.running(conversion={"job_id": "j9", "state": "running", "solution_name": "Invoices"})
    attaches = []
    monkeypatch.setattr(cc, "cmd_attach", lambda host, port, **kw: attaches.append(port) or 0)
    out = io.StringIO()
    code = cc.handle_port_clash("127.0.0.1", companion.port, replace=False, no_attach=False,
                                is_tty=True, out=out)
    assert code == 0 and attaches == [companion.port]
    text = out.getvalue()
    assert "already in use by a companion server" in text
    assert str(companion.pid) in text and "Invoices (job j9, running)" in text
    assert "another folder" not in text


def test_clash_no_attach_prints_details_and_exits_1(companion, monkeypatch):
    companion.running()
    monkeypatch.setattr(cc, "cmd_attach", lambda *a, **kw: pytest.fail("attached"))
    out = io.StringIO()
    assert cc.handle_port_clash("127.0.0.1", companion.port, replace=False, no_attach=True,
                                is_tty=True, out=out) == 1
    assert str(companion.pid) in out.getvalue() and "--no-attach" in out.getvalue()


def test_clash_without_a_terminal_does_not_attach(companion, monkeypatch):
    companion.running()
    monkeypatch.setattr(cc, "cmd_attach", lambda *a, **kw: pytest.fail("attached"))
    out = io.StringIO()     # not a tty
    assert cc.handle_port_clash("127.0.0.1", companion.port, replace=False, no_attach=False,
                                out=out) == 1
    assert "No terminal" in out.getvalue()


def test_clash_replace_stops_it_and_says_start(companion, procs):
    companion.running()
    out = io.StringIO()
    assert cc.handle_port_clash("127.0.0.1", companion.port, replace=True, no_attach=False,
                                is_tty=False, out=out) is None
    assert companion.shutdown_calls and "Stopped." in out.getvalue()


def test_clash_replace_refused_mid_conversion(companion):
    job = {"job_id": "j1", "state": "running", "solution_name": "Invoices"}
    companion.running(conversion=job)
    companion.shutdown_reply = (409, {"error": "conversion running", "job": job})
    assert cc.handle_port_clash("127.0.0.1", companion.port, replace=True, no_attach=False,
                                is_tty=True, out=io.StringIO()) == 2


def test_clash_with_another_program_names_it(procs, free_port):
    procs.owner = {"pid": 1234, "program": "node.exe"}
    out = io.StringIO()
    assert cc.handle_port_clash("127.0.0.1", free_port, replace=True, no_attach=False,
                                is_tty=True, out=out) == 1
    text = out.getvalue()
    assert "node.exe (PID 1234)" in text
    assert "probably by another companion" not in text
    assert procs.terminated == []


def test_clash_with_an_unknown_program_keeps_the_hint(procs, free_port):
    out = io.StringIO()
    assert cc.handle_port_clash("127.0.0.1", free_port, replace=False, no_attach=False,
                                is_tty=True, out=out) == 1
    assert ("findstr" if os.name == "nt" else "lsof") in out.getvalue()


def test_clash_with_a_stale_state_file_says_so(procs, free_port):
    procs.owner = {"pid": 1234, "program": "nginx"}
    procs.state = {"pid": 1234, "port": free_port, "token": "t"}     # PID reused by nginx
    procs.alive.add(1234)
    out = io.StringIO()
    assert cc.handle_port_clash("127.0.0.1", free_port, replace=False, no_attach=False,
                                is_tty=True, out=out) == 1
    text = out.getvalue()
    assert "nginx (PID 1234)" in text
    assert "stale (pid reused by another program" in text
    assert "Removed the stale state file" in text and procs.removed == [1234]


def test_clash_with_a_companion_from_another_folder(companion, monkeypatch):
    other = os.path.join(os.path.dirname(cc.AFM_ROOT), "some-other-agentic-fm")
    companion.running(afm_root=other)
    monkeypatch.setattr(cc, "cmd_attach", lambda host, port, **kw: 0)
    out = io.StringIO()
    cc.handle_port_clash("127.0.0.1", companion.port, replace=False, no_attach=False, is_tty=True, out=out)
    assert "companion from another folder: %s" % other in out.getvalue()


def test_clash_state_file_names_another_companion(companion, procs, monkeypatch):
    companion.running()
    procs.state = dict(procs.state, pid=999)    # state file from another (living) companion
    procs.alive.add(999)
    procs.companions.add(999)
    monkeypatch.setattr(cc, "cmd_attach", lambda host, port, **kw: 0)
    out = io.StringIO()
    cc.handle_port_clash("127.0.0.1", companion.port, replace=False, no_attach=True, is_tty=True, out=out)
    assert "names another companion (PID 999" in out.getvalue()


def test_clash_with_our_companion_not_answering(procs, free_port):
    procs.state = {"pid": 66, "port": free_port, "token": "t"}
    procs.alive.add(66)
    procs.companions.add(66)
    procs.owner = {"pid": 66, "program": "python.exe"}
    out = io.StringIO()
    assert cc.handle_port_clash("127.0.0.1", free_port, replace=False, no_attach=False,
                                is_tty=True, out=out) == 1
    assert "doesn't answer" in out.getvalue()
    assert cc.handle_port_clash("127.0.0.1", free_port, replace=True, no_attach=False,
                                is_tty=True, out=io.StringIO()) is None
    assert procs.terminated == [66]


# ---------------------------------------------------------------------------
# With the real companion_state (state folder under tmp_path)
# ---------------------------------------------------------------------------

def test_real_state_stop_removes_a_dead_companions_file(tmp_path, monkeypatch, free_port):
    monkeypatch.setenv(companion_state.STATE_DIR_ENV, str(tmp_path))
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    path = companion_state.write_state({"pid": finished.pid, "port": free_port, "token": "t"})
    assert companion_state.check_state()["stale"]
    out = io.StringIO()
    assert cc.cmd_stop("127.0.0.1", free_port, out=out) == 0
    assert not os.path.exists(path)
    assert "Removed the stale state file" in out.getvalue()


def test_real_port_owner_names_the_program(tmp_path, monkeypatch):
    monkeypatch.setenv(companion_state.STATE_DIR_ENV, str(tmp_path))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        out = io.StringIO()
        assert cc.handle_port_clash("127.0.0.1", port, replace=True, no_attach=False,
                                    is_tty=True, out=out) == 1
    finally:
        sock.close()
    text = out.getvalue()
    assert "not a companion server" in text or "could not be found" in text
    if "not a companion server" in text:
        assert "(PID %d)" % os.getpid() in text
