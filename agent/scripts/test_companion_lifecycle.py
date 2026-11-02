"""test_companion_lifecycle.py - finding and stopping the companion (roadmap P3-18 to P3-21, server side).

The log file and its levels (P3-18), GET /health and the state file (P3-19), POST /shutdown,
the subcommands and the Windows-safe cleanup (P3-20), and main()'s full start and stop. Every
test uses a test state folder ($COMPANION_STATE_DIR), never the real per-user one.
"""

from __future__ import annotations

import io
import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

import companion_live as cl
import companion_server as cs


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """A test state folder, no companion.json, no env overrides, no update check, no jobs."""
    monkeypatch.setenv("COMPANION_STATE_DIR", str(tmp_path / "state"))
    for name in (cs.LOG_FILE_ENV, cs.LAUNCHED_BY_ENV, "COMPANION_BIND_HOST", "COMPANION_PORT",
                 "COMPANION_IDLE_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cs, "_load_companion_config", lambda: {})
    monkeypatch.setattr(cs, "_check_for_updates", lambda: None)
    monkeypatch.setattr(cs, "_check_plugin", lambda force=False: {"installed": False, "usable": False})
    monkeypatch.setattr(cs, "_state_token", "")
    monkeypatch.setattr(cs, "_stopping", False)
    monkeypatch.setattr(cs, "_shutdown_reason", "")
    monkeypatch.setattr(cs, "_server_info", dict(cs._server_info))
    with cs._convert_lock:
        cs._convert_jobs.clear()
    yield
    cs._teardown_file_logging()
    with cs._convert_lock:
        cs._convert_jobs.clear()


@pytest.fixture
def server():
    """A companion server on a free port in a thread; yields (httpd, base url, thread)."""
    httpd = cs.ThreadingHTTPServer(("127.0.0.1", 0), cs.CompanionHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd, f"http://127.0.0.1:{httpd.server_address[1]}", thread
    finally:
        httpd.shutdown()
        httpd.server_close()


def _request(url: str, body: "dict | None" = None, headers: "dict | None" = None,
             method: "str | None" = None) -> tuple:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data is not None else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def _token(value: str) -> dict:
    return {cs.companion_state.TOKEN_HEADER: value}


def _add_job(job_id: str = "job1", state: str = "running") -> None:
    with cs._convert_lock:
        cs._convert_jobs[job_id] = {"job_id": job_id, "state": state, "done": False,
                                    "solution_name": "Invoices", "output_path": ""}


def _finish_job(job_id: str = "job1") -> None:
    with cs._convert_lock:
        cs._convert_jobs[job_id].update(state="succeeded", done=True)


# -- P3-18 log file ------------------------------------------------------------------

def test_log_file_default_is_in_the_state_folder(tmp_path):
    path, source = cs._resolve_log_file(None, False, {}, environ={})
    assert source == "default"
    assert path == os.path.join(str(tmp_path / "state"), "companion-server.log")


@pytest.mark.parametrize("cli, env, comp, expected, source", [
    ("cli.log", "env.log", "json.log", "cli.log", "--log-file"),
    (None, "env.log", "json.log", "env.log", "$COMPANION_LOG_FILE"),
    (None, None, "json.log", "json.log", "companion.json log_file"),
    (None, "", "json.log", "", "$COMPANION_LOG_FILE"),       # empty env turns it off
    (None, None, "off", "", "companion.json log_file"),      # so does "off"
    (None, "OFF", None, "", "$COMPANION_LOG_FILE"),
])
def test_log_file_precedence(tmp_path, cli, env, comp, expected, source):
    environ = {} if env is None else {cs.LOG_FILE_ENV: env}
    config = {} if comp is None else {"log_file": comp}
    path, got_source = cs._resolve_log_file(cli, False, config, environ=environ)
    assert got_source == source
    assert path == (os.path.abspath(expected) if expected else "")


def test_no_log_file_wins_over_everything():
    path, source = cs._resolve_log_file("cli.log", True, {"log_file": "json.log"},
                                        environ={cs.LOG_FILE_ENV: "env.log"})
    assert (path, source) == ("", "--no-log-file")


@pytest.fixture
def console():
    """A console handler on the root logger, like basicConfig's, writing to a buffer."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    root.addHandler(handler)
    yield stream
    root.removeHandler(handler)


def test_file_gets_debug_lines_and_console_stays_at_info(tmp_path, console):
    path = tmp_path / "logs" / "companion-server.log"
    handler = cs._setup_file_logging(str(path))
    assert handler is not None and handler in logging.getLogger().handlers
    cs.log.debug("a debug poll line")
    cs.log.info("an info line")
    handler.flush()
    written = path.read_text(encoding="utf-8")
    assert "DEBUG companion_server: a debug poll line" in written
    assert "INFO companion_server: an info line" in written
    assert "an info line" in console.getvalue() and "a debug poll line" not in console.getvalue()
    cs._teardown_file_logging()
    assert handler not in logging.getLogger().handlers and cs.log.level == logging.NOTSET


def test_file_handler_rotates_by_size(tmp_path):
    handler = cs._setup_file_logging(str(tmp_path / "companion-server.log"))
    assert isinstance(handler, logging.handlers.RotatingFileHandler)
    assert handler.maxBytes == cs.companion_state.LOG_MAX_BYTES
    assert handler.backupCount == cs.companion_state.LOG_BACKUP_COUNT


def test_subprocess_lines_reach_the_file(tmp_path):
    path = tmp_path / "companion-server.log"
    cs._setup_file_logging(str(path))
    cs._run_command_streaming([sys.executable, "-c", "import sys; print('out line'); "
                               "sys.stderr.write('err line\\n')"],
                              cwd=None, env=None, label="fake")
    written = path.read_text(encoding="utf-8")
    assert "[fake stdout] out line" in written and "[fake stderr] err line" in written


def test_file_keeps_receiving_lines_while_the_live_panel_is_drawn(tmp_path, console):
    from rich.console import Console

    path = tmp_path / "companion-server.log"
    cs._setup_file_logging(str(path))
    out = io.StringIO()
    view = cl.LiveView(lambda: {}, list, console=Console(file=out, force_terminal=True, width=100),
                       refresh_per_second=20)
    view.start()
    try:
        cs.log.info("info while the panel is drawn")
        cs.log.debug("debug while the panel is drawn")
        time.sleep(0.1)
    finally:
        view.stop()
    cs.log.info("info after the panel")
    written = path.read_text(encoding="utf-8")
    for line in ("info while the panel", "debug while the panel", "info after the panel"):
        assert line in written
    assert "info while the panel" in out.getvalue() and "debug while the panel" not in out.getvalue()


def test_an_unopenable_log_file_falls_back_to_the_console(tmp_path, caplog):
    blocker = tmp_path / "a file"
    blocker.write_text("", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        assert cs._setup_file_logging(str(blocker / "companion-server.log")) is None
    assert "Could not open the log file" in caplog.text


# -- P3-19 /health and the state file --------------------------------------------------

HEALTH_FIELDS = {"status", "version", "plugin", "platform", "pid", "started_at", "uptime_seconds", "host",
                 "port", "afm_root", "python", "log_file", "launched_by", "conversion", "viewers"}


def test_health_says_who_the_companion_is(server):
    httpd, base, _ = server
    cs._init_server_info("127.0.0.1", httpd.server_address[1], "C:/logs/companion-server.log", "ide")
    status, health = _request(f"{base}/health")
    assert status == 200 and set(health) == HEALTH_FIELDS
    assert health["status"] == "ok" and health["pid"] == os.getpid()
    assert health["platform"] == {"darwin": "macos", "win32": "windows"}.get(sys.platform, "linux")
    assert health["port"] == httpd.server_address[1] and health["host"] == "127.0.0.1"
    assert health["afm_root"] == cs.AFM_ROOT and health["python"] == sys.executable
    assert health["log_file"] == "C:/logs/companion-server.log" and health["launched_by"] == "ide"
    assert isinstance(health["uptime_seconds"], float) and health["conversion"] is None
    assert "token" not in health


def test_health_without_main_uses_the_server_address(server):
    httpd, base, _ = server
    cs._server_info["port"] = None
    health = _request(f"{base}/health")[1]
    assert health["port"] == httpd.server_address[1]


def test_health_shows_the_running_conversion_and_is_not_activity(server):
    _, base, _ = server
    _add_job("queued1", "queued")
    _add_job("running1", "running")
    before = cs._last_activity
    health = _request(f"{base}/health")[1]
    assert health["conversion"]["job_id"] == "running1"
    assert cs._last_activity == before


def test_launched_by_flag_then_env_then_terminal(monkeypatch):
    assert cs._launched_by(None, environ={}) == "terminal"
    assert cs._launched_by(None, environ={cs.LAUNCHED_BY_ENV: "logon task"}) == "logon task"
    assert cs._launched_by("launchd", environ={cs.LAUNCHED_BY_ENV: "background"}) == "launchd"


def test_state_file_has_the_fields_and_a_token(tmp_path):
    cs._init_server_info("127.0.0.1", 8765, "", "background")
    path = cs._write_state_file()
    assert path == os.path.join(str(tmp_path / "state"), "companion.state.json")
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert data["token"] == cs._state_token and len(data["token"]) >= 32
    assert {k: data[k] for k in ("pid", "host", "port", "launched_by", "log_file")} == {
        "pid": os.getpid(), "host": "127.0.0.1", "port": 8765, "launched_by": "background", "log_file": None}
    assert {"started_at", "afm_root", "python"} <= set(data)
    assert not {"plugin", "uptime_seconds", "conversion", "status"} & set(data)


def test_a_state_file_that_cannot_be_written_is_only_a_warning(monkeypatch, caplog):
    def fail(fields, path=None):
        raise PermissionError("denied")

    monkeypatch.setattr(cs.companion_state, "write_state", fail)
    with caplog.at_level(logging.WARNING, logger="companion_server"):
        assert cs._write_state_file() is None
    assert "Could not write the state file" in caplog.text


# -- P3-20 POST /shutdown ----------------------------------------------------------------

def _stopped(thread, timeout: float = 5.0) -> bool:
    thread.join(timeout)
    return not thread.is_alive()


@pytest.mark.parametrize("headers", [{}, _token("wrong")])
def test_shutdown_needs_the_token(server, monkeypatch, headers):
    _, base, thread = server
    monkeypatch.setattr(cs, "_state_token", "the-right-token")
    status, body = _request(f"{base}/shutdown", {}, headers=headers)
    assert status == 403 and "X-Companion-Token" in body["error"]
    assert not _stopped(thread, 0.7)


def test_refused_shutdown_reads_the_body_so_the_403_arrives(server, monkeypatch):
    # Unread body + close = a reset on Windows (WinError 10053) instead of the 403; send a big one
    _, base, _ = server
    monkeypatch.setattr(cs, "_state_token", "tok")
    for _ in range(20):
        status, body = _request(f"{base}/shutdown", {"pad": "x" * 200_000}, headers=_token("wrong"))
        assert status == 403 and "X-Companion-Token" in body["error"]


def test_shutdown_is_refused_when_there_is_no_token_at_all(server):
    _, base, _ = server
    assert _request(f"{base}/shutdown", {}, headers=_token(""))[0] == 403


def test_shutdown_stops_the_server(server, monkeypatch):
    _, base, thread = server
    monkeypatch.setattr(cs, "_state_token", "tok")
    status, body = _request(f"{base}/shutdown", None, headers=_token("tok"), method="POST")
    assert (status, body) == (202, {"status": "stopping"})
    assert _stopped(thread)


def test_shutdown_while_converting_is_409_with_the_job(server, monkeypatch):
    _, base, thread = server
    monkeypatch.setattr(cs, "_state_token", "tok")
    _add_job()
    status, body = _request(f"{base}/shutdown", {}, headers=_token("tok"))
    assert status == 409 and body["error"] == "conversion running" and body["job"]["job_id"] == "job1"
    assert not _stopped(thread, 0.7) and cs._stopping is False


@pytest.mark.parametrize("how", ["body", "query"])
def test_shutdown_force_stops_mid_conversion(server, monkeypatch, how):
    _, base, thread = server
    monkeypatch.setattr(cs, "_state_token", "tok")
    _add_job()
    url, body = (f"{base}/shutdown", {"force": True}) if how == "body" else (f"{base}/shutdown?force=1", None)
    status, reply = _request(url, body, headers=_token("tok"), method="POST")
    assert (status, reply) == (202, {"status": "stopping"})
    assert _stopped(thread)
    assert cs._shutdown_reason == "stopped by /shutdown"


def test_shutdown_wait_stops_once_the_job_is_done(server, monkeypatch, tmp_path):
    _, base, thread = server
    monkeypatch.setattr(cs, "_state_token", "tok")
    _add_job()
    status, reply = _request(f"{base}/shutdown", {"wait": True}, headers=_token("tok"))
    assert status == 202 and reply["status"] == "stopping after job" and reply["job"]["job_id"] == "job1"
    assert not _stopped(thread, 1.0)
    # New conversions are refused meanwhile
    status, refused = _request(f"{base}/convert", {"solution_name": "X", "export_path": str(tmp_path)})
    assert status == 503 and "stopping" in refused["error"]
    _finish_job()
    assert _stopped(thread)


def test_shutdown_with_bad_json_is_400(server, monkeypatch):
    _, base, _ = server
    monkeypatch.setattr(cs, "_state_token", "tok")
    req = urllib.request.Request(f"{base}/shutdown", data=b"{not json", method="POST", headers=_token("tok"))
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(req, timeout=10)
    assert info.value.code == 400


# -- main(): start, state file, POST /shutdown through the finally, log file --------------

def _run_main(argv: list) -> threading.Thread:
    thread = threading.Thread(target=cs.main, args=(argv,), daemon=True)
    thread.start()
    return thread


def _wait_for_state(path: str, timeout: float = 10) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        data = cs.companion_state.read_state(path)
        if data and data.get("pid") == os.getpid():
            return data
        time.sleep(0.05)
    raise AssertionError("main() never wrote its state file")


def test_main_writes_the_state_file_and_post_shutdown_goes_through_the_finally(tmp_path, monkeypatch):
    stopped_webviewer = []
    monkeypatch.setattr(cs, "_shutdown_cleanup",
                        lambda server: (stopped_webviewer.append(True), server.server_close()))
    state_path = os.path.join(str(tmp_path / "state"), "companion.state.json")
    thread = _run_main(["--port", "0", "--no-live", "--launched-by", "ide"])
    state = _wait_for_state(state_path)
    base = f"http://127.0.0.1:{state['port']}"
    assert state["port"] > 0 and state["launched_by"] == "ide"
    assert state["log_file"] == os.path.join(str(tmp_path / "state"), "companion-server.log")

    health = _request(f"{base}/health")[1]
    assert health["pid"] == os.getpid() and health["port"] == state["port"]
    assert health["log_file"] == state["log_file"] and health["started_at"] == state["started_at"]

    assert _request(f"{base}/shutdown", {}, headers=_token("nope"))[0] == 403
    assert _request(f"{base}/shutdown", {}, headers=_token(state["token"]))[0] == 202
    assert _stopped(thread, 10)
    assert stopped_webviewer == [True]
    assert not os.path.exists(state_path)
    written = open(state["log_file"], encoding="utf-8").read()
    assert f"listening on 127.0.0.1:{state['port']} (PID {os.getpid()})" in written
    assert f"Log file: {state['log_file']}" in written
    assert "DEBUG companion_server: State file:" in written
    assert "POST /shutdown" in written and "Stopped." in written
    assert cs._file_handler is None


def test_main_finally_leaves_a_newer_companions_state_file(tmp_path, monkeypatch):
    state_path = os.path.join(str(tmp_path / "state"), "companion.state.json")

    def serve(self, *args, **kwargs):     # another companion took over the state file meanwhile
        cs.companion_state.write_state({"pid": os.getpid() + 1, "token": "x"})
        raise KeyboardInterrupt

    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever", serve)
    cs.main(["--port", "0", "--no-live", "--no-log-file"])
    assert cs.companion_state.read_state(state_path)["pid"] == os.getpid() + 1


def test_main_marks_a_running_conversion_failed_on_shutdown(tmp_path, monkeypatch):
    def serve(self, *args, **kwargs):
        _add_job("jobX")
        raise KeyboardInterrupt

    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever", serve)
    cs.main(["--port", "0", "--no-live", "--no-log-file"])
    job = cs._get_convert_job("jobX")
    assert job["state"] == "failed" and "shut down" in job["error"]
    assert (tmp_path / "state" / "jobs" / "jobX.json").exists()


def test_main_with_no_log_file_writes_none(tmp_path, monkeypatch):
    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever",
                        lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    cs.main(["--port", "0", "--no-live", "--no-log-file"])
    assert not (tmp_path / "state" / "companion-server.log").exists()


def test_main_sets_the_terminal_title(monkeypatch):
    titles = []
    monkeypatch.setattr(cs.companion_state, "set_terminal_title", lambda text, stream=None: titles.append(text))
    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever",
                        lambda self, *a, **k: (_ for _ in ()).throw(KeyboardInterrupt))
    cs.main(["--port", "0", "--no-live", "--no-log-file"])
    assert len(titles) == 1
    assert titles[0].startswith("agentic-fm companion :") and titles[0].endswith(f"(PID {os.getpid()})")


# -- P3-20 subcommands ---------------------------------------------------------------------

def test_no_subcommand_means_start_and_old_flags_still_work():
    args = cs.parse_args(["--port", "9000", "--idle-timeout", "5", "--no-live"])
    assert (args.command, args.port, args.idle_timeout, args.no_live) == ("start", 9000, 5, True)
    assert args.timeout == cs.DEFAULT_STOP_TIMEOUT and args.force is False
    assert args.log_file is None and args.no_log_file is False and args.launched_by is None


@pytest.mark.parametrize("argv, command", [
    (["status"], "status"), (["stop", "--timeout", "3", "--force"], "stop"),
    (["--port", "9001", "restart"], "restart"), (["attach"], "attach"),
    (["start", "--replace", "--no-attach", "--log-file", "x.log", "--launched-by", "launchd"], "start"),
])
def test_subcommands_parse(argv, command):
    args = cs.parse_args(argv)
    assert args.command == command
    if command == "stop":
        assert args.timeout == 3.0 and args.force is True
    if command == "start":
        assert args.replace and args.no_attach and args.log_file == "x.log" and args.launched_by == "launchd"


def test_unknown_subcommand_is_an_error(capsys):
    with pytest.raises(SystemExit) as info:
        cs.parse_args(["explode"])
    assert info.value.code == 2


@pytest.mark.parametrize("command, function, kwargs", [
    ("status", "cmd_status", {}),
    ("stop", "cmd_stop", {"timeout": 4.0, "force": True}),
    ("attach", "cmd_attach", {}),
])
def test_subcommands_dispatch_to_companion_control(monkeypatch, command, function, kwargs):
    calls = []
    monkeypatch.setattr(cs.companion_control, function,
                        lambda host, port, **kw: (calls.append((host, port, kw)), 7)[1])
    argv = [command, "--port", "9100"] + (["--timeout", "4", "--force"] if command == "stop" else [])
    with pytest.raises(SystemExit) as info:
        cs.main(argv)
    assert info.value.code == 7
    assert calls == [("127.0.0.1", 9100, kwargs)]


def test_control_commands_reach_a_wildcard_bind_on_loopback(monkeypatch):
    monkeypatch.setenv("COMPANION_BIND_HOST", "0.0.0.0")
    calls = []
    monkeypatch.setattr(cs.companion_control, "cmd_status", lambda host, port, **kw: (calls.append(host), 0)[1])
    with pytest.raises(SystemExit):
        cs.main(["status"])
    assert calls == ["127.0.0.1"]
    assert cs._client_host("::") == "::1" and cs._client_host("10.0.0.5") == "10.0.0.5"


def test_restart_stops_then_starts_here(monkeypatch):
    order = []
    monkeypatch.setattr(cs.companion_control, "cmd_stop", lambda host, port, **kw: (order.append("stop"), 0)[1])

    def serve(self, *args, **kwargs):
        order.append("serve")
        raise KeyboardInterrupt

    monkeypatch.setattr(cs.ThreadingHTTPServer, "serve_forever", serve)
    cs.main(["restart", "--port", "0", "--no-live", "--no-log-file"])
    assert order == ["stop", "serve"]


@pytest.mark.parametrize("code", [1, 2])      # 1 could not stop it, 2 refused (conversion running)
def test_restart_does_not_start_when_stop_failed(monkeypatch, code):
    monkeypatch.setattr(cs.companion_control, "cmd_stop", lambda host, port, **kw: code)
    monkeypatch.setattr(cs, "_bind_server", lambda *a: pytest.fail("restart started after a failed stop"))
    with pytest.raises(SystemExit) as info:
        cs.main(["restart", "--port", "0"])
    assert info.value.code == code


# -- P3-20 T4 cleanup per platform ---------------------------------------------------------

class FakeWebviewer:
    pid = 31337

    def __init__(self):
        self.terminated = False

    def poll(self):
        return None

    def terminate(self):
        self.terminated = True


class FakeServer:
    closed = False

    def server_close(self):
        self.closed = True


def test_shutdown_cleanup_on_windows_never_calls_getpgid(monkeypatch):
    def forbidden(*args):
        raise AssertionError("os.getpgid/os.killpg called on Windows")

    runs = []
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs.os, "getpgid", forbidden, raising=False)
    monkeypatch.setattr(cs.os, "killpg", forbidden, raising=False)
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda cmd, **kw: (runs.append(cmd), subprocess.CompletedProcess(cmd, 0))[1])
    proc = FakeWebviewer()
    monkeypatch.setattr(cs, "_webviewer_proc", proc)
    server = FakeServer()
    cs._shutdown_cleanup(server)
    assert server.closed
    assert runs == [["taskkill", "/PID", "31337", "/T", "/F"]] and not proc.terminated


def test_windows_cleanup_falls_back_to_terminate(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 128))
    proc = FakeWebviewer()
    assert cs._stop_process_tree(proc) == "process 31337" and proc.terminated


def test_shutdown_cleanup_on_posix_signals_the_process_group(monkeypatch):
    signalled = []
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(cs.os, "getpgid", lambda pid: pid + 1, raising=False)
    monkeypatch.setattr(cs.os, "killpg", lambda pgid, sig: signalled.append((pgid, sig)), raising=False)
    monkeypatch.setattr(cs, "_webviewer_proc", FakeWebviewer())
    cs._shutdown_cleanup(FakeServer())
    assert signalled == [(31338, signal.SIGTERM)]


# -- SIGTERM (and SIGBREAK on Windows) stop like Ctrl-C --------------------------------------

def test_sigterm_goes_through_the_finally_and_main_returns(tmp_path, monkeypatch):
    installed = {}

    def fake_signal(signum, handler):     # record instead of touching the test process's handlers
        previous = installed.get(signum, signal.SIG_DFL)
        installed[signum] = handler
        return previous

    monkeypatch.setattr(cs.signal, "signal", fake_signal)
    state_path = os.path.join(str(tmp_path / "state"), "companion.state.json")

    def send_sigterm():
        _wait_for_state(state_path)
        installed[signal.SIGTERM](signal.SIGTERM, None)

    sender = threading.Thread(target=send_sigterm, daemon=True)
    sender.start()
    cs.main(["--port", "0", "--no-live"])     # returns normally: the process would exit 0
    sender.join(5)
    assert signal.SIGTERM in installed
    if sys.platform == "win32":
        assert signal.SIGBREAK in installed
    assert not os.path.exists(state_path)
    written = (tmp_path / "state" / "companion-server.log").read_text(encoding="utf-8")
    assert "Got SIGTERM; shutting down." in written and "Stopped." in written
    assert installed[signal.SIGTERM] is signal.SIG_DFL     # restored in the finally


def test_stop_signals_are_left_alone_off_the_main_thread(monkeypatch):
    monkeypatch.setattr(cs.signal, "signal", lambda *a: pytest.fail("set a handler off the main thread"))
    result = []
    thread = threading.Thread(target=lambda: result.append(cs._install_stop_signals(FakeServer())))
    thread.start()
    thread.join(5)
    assert result == [{}]
