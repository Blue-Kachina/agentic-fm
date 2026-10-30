"""test_companion_state.py - the companion's state folder, log file, state file and process helpers (P3-18, P3-19).

Every test points COMPANION_STATE_DIR at tmp_path, so the real per-user folder is never touched.
Process tests run real child processes on this platform; the other platforms' branches run
through monkeypatched helpers, so the whole file runs on Windows, macOS and Linux.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import time

import pytest

import companion_state as cs

# On Windows a venv's python.exe is a launcher that starts the real interpreter as a child;
# the base interpreter keeps a child to one process, so terminating it leaves nothing behind.
CHILD_PYTHON = getattr(sys, "_base_executable", None) or sys.executable


@pytest.fixture(autouse=True)
def state_env(tmp_path, monkeypatch):
    folder = tmp_path / "state"
    monkeypatch.setenv(cs.STATE_DIR_ENV, str(folder))
    return folder


def _spawn(tmp_path, name: str, seconds: int = 30) -> subprocess.Popen:
    script = tmp_path / name
    script.write_text(f"import time\ntime.sleep({seconds})\n", encoding="utf-8")
    return subprocess.Popen([CHILD_PYTHON, str(script)])


@pytest.fixture
def children():
    procs = []
    yield procs
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)


def _wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


# ---------------------------------------------------------------------------
# State folder
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = str(tmp_path / "home")
    monkeypatch.setattr(cs.os.path, "expanduser", lambda p: p.replace("~", home, 1))
    return home


def test_state_dir_windows(monkeypatch, fake_home):
    monkeypatch.delenv(cs.STATE_DIR_ENV)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", os.path.join("C:", "Users", "me", "AppData", "Local"))
    assert cs.state_dir(create=False) == os.path.join("C:", "Users", "me", "AppData", "Local", "agentic-fm")
    monkeypatch.delenv("LOCALAPPDATA")
    assert cs.state_dir(create=False) == os.path.join(fake_home, "AppData", "Local", "agentic-fm")


def test_state_dir_mac(monkeypatch, fake_home):
    monkeypatch.delenv(cs.STATE_DIR_ENV)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert cs.state_dir(create=False) == os.path.join(fake_home, "Library", "Logs", "agentic-fm")


def test_state_dir_linux(monkeypatch, fake_home, tmp_path):
    monkeypatch.delenv(cs.STATE_DIR_ENV)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert cs.state_dir(create=False) == os.path.join(fake_home, ".local", "state", "agentic-fm")
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert cs.state_dir(create=False) == os.path.join(str(tmp_path / "xdg"), "agentic-fm")


def test_state_dir_override_wins_and_is_created(state_env, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin" if os.name == "posix" else "win32")
    assert cs.state_dir(create=False) == str(state_env)
    assert not state_env.exists()
    assert cs.state_dir() == str(state_env)
    assert state_env.is_dir()
    if os.name == "posix":
        assert (state_env.stat().st_mode & 0o777) == 0o700


def test_paths_live_in_the_state_folder(state_env):
    assert cs.default_log_file() == os.path.join(str(state_env), "companion-server.log")
    assert cs.state_file_path() == os.path.join(str(state_env), "companion.state.json")
    assert cs.jobs_dir(create=False) == os.path.join(str(state_env), "jobs")
    assert not (state_env / "jobs").exists()
    assert os.path.isdir(cs.jobs_dir())


# ---------------------------------------------------------------------------
# Log file
# ---------------------------------------------------------------------------

def _logger(handler) -> logging.Logger:
    logger = logging.getLogger(f"test_companion_state.{id(handler)}")
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return logger


def test_file_handler_defaults_and_debug_lines(tmp_path):
    path = tmp_path / "sub" / "companion-server.log"
    handler = cs.make_file_handler(str(path))
    try:
        assert handler.maxBytes == cs.LOG_MAX_BYTES == 5 * 1024 * 1024
        assert handler.backupCount == cs.LOG_BACKUP_COUNT == 5
        assert handler.level == logging.DEBUG
        assert path.parent.is_dir()
        logger = _logger(handler)
        logger.debug("stage progress: Café")
        handler.flush()
    finally:
        handler.close()
    text = path.read_text(encoding="utf-8")
    assert "DEBUG" in text and "stage progress: Café" in text


def test_file_handler_rotates_by_size(tmp_path):
    path = tmp_path / "companion-server.log"
    handler = cs.make_file_handler(str(path))
    handler.maxBytes = 300
    try:
        logger = _logger(handler)
        for i in range(200):
            logger.info("line %03d %s", i, "x" * 40)
    finally:
        handler.close()
    backups = sorted(p.name for p in tmp_path.iterdir() if p.name.startswith("companion-server.log."))
    assert backups == [f"companion-server.log.{n}" for n in range(1, cs.LOG_BACKUP_COUNT + 1)]
    assert path.stat().st_size <= 400
    assert "line 199" in path.read_text(encoding="utf-8")


def test_failed_rollover_keeps_logging(tmp_path, monkeypatch):
    path = tmp_path / "companion-server.log"
    handler = cs.make_file_handler(str(path))
    handler.maxBytes = 100

    def refuse(self):
        if self.stream:
            self.stream.close()
            self.stream = None
        raise PermissionError("held open by another process")

    monkeypatch.setattr(cs.RotatingFileHandler, "doRollover", refuse)
    try:
        logger = _logger(handler)
        for i in range(20):
            logger.info("line %02d %s", i, "y" * 40)
        assert handler._retry_after > time.monotonic()
    finally:
        handler.close()
    text = path.read_text(encoding="utf-8")
    assert "line 00" in text and "line 19" in text


# ---------------------------------------------------------------------------
# State file
# ---------------------------------------------------------------------------

def test_new_token_is_random_and_long():
    first, second = cs.new_token(), cs.new_token()
    assert first != second and len(first) >= 40


def test_write_read_remove_state(state_env):
    fields = {"pid": 1234, "port": 8765, "token": "secret", "afm_root": "C:\\agentic-fm é"}
    path = cs.write_state(fields)
    assert path == cs.state_file_path()
    assert cs.read_state() == fields
    assert [p.name for p in state_env.iterdir()] == ["companion.state.json"]   # no temp file left
    if os.name == "posix":
        assert (os.stat(path).st_mode & 0o777) == 0o600
    assert cs.remove_state(pid=999) is False          # another companion's file stays
    assert os.path.exists(path)
    assert cs.remove_state(pid=1234) is True
    assert not os.path.exists(path)
    assert cs.read_state() is None
    assert cs.remove_state() is False


def test_write_state_is_atomic(tmp_path, monkeypatch):
    path = str(tmp_path / "s.json")
    cs.write_state({"pid": 1}, path)
    calls = []
    real_replace = os.replace

    def spy(src, dst):
        calls.append((src, dst))
        assert cs.read_state(path) == {"pid": 1}      # the old file is intact until the replace
        real_replace(src, dst)

    monkeypatch.setattr(cs.os, "replace", spy)
    cs.write_state({"pid": 2}, path)
    assert len(calls) == 1 and calls[0][1] == path and calls[0][0] != path
    assert cs.read_state(path) == {"pid": 2}


def test_remove_state_without_pid(tmp_path):
    path = str(tmp_path / "s.json")
    cs.write_state({"pid": 5}, path)
    assert cs.remove_state(path) is True
    assert not os.path.exists(path)


def test_malformed_state_file(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json", encoding="utf-8")
    assert cs.read_state(str(path)) is None
    assert cs.remove_state(str(path), pid=1) is False
    assert path.exists()
    path.write_text("[1, 2]", encoding="utf-8")
    assert cs.read_state(str(path)) is None


def test_read_state_does_not_create_the_folder(state_env):
    assert cs.read_state() is None
    assert cs.remove_state(pid=1) is False
    assert not state_env.exists()


# ---------------------------------------------------------------------------
# Processes on this platform (real child processes)
# ---------------------------------------------------------------------------

def test_process_alive_and_companion_check(tmp_path, children):
    companion = _spawn(tmp_path, "companion_server.py")
    other = _spawn(tmp_path, "something_else.py")
    children.extend([companion, other])
    assert cs.process_alive(os.getpid())
    assert cs.process_alive(companion.pid)
    assert _wait_until(lambda: "companion_server.py" in (cs.process_command_line(companion.pid) or ""))
    assert cs.is_companion_process(companion.pid)
    assert not cs.is_companion_process(other.pid)
    assert "something_else.py" in (cs.process_command_line(other.pid) or "")
    assert not cs.is_companion_process(os.getpid())


def test_dead_process(tmp_path):
    proc = subprocess.Popen([CHILD_PYTHON, "-c", "pass"])
    proc.wait(timeout=30)
    assert not cs.process_alive(proc.pid)
    assert not cs.is_companion_process(proc.pid)
    assert not cs.process_alive(0)
    assert not cs.process_alive(-1)
    assert not cs.process_alive("not a pid")


def test_check_state_cases(tmp_path, children):
    assert cs.check_state() == {"state": None, "running": False, "stale": False, "reason": "no state file"}

    companion = _spawn(tmp_path, "companion_server.py")
    other = _spawn(tmp_path, "notepad_like.py")
    children.extend([companion, other])
    _wait_until(lambda: cs.is_companion_process(companion.pid))

    cs.write_state({"pid": companion.pid, "port": 8765})
    result = cs.check_state()
    assert result["running"] and not result["stale"] and result["reason"] == "running"
    assert result["state"]["port"] == 8765

    reused = cs.check_state({"pid": other.pid})
    assert reused == {"state": {"pid": other.pid}, "running": False, "stale": True,
                      "reason": "pid reused by another program"}

    dead = subprocess.Popen([CHILD_PYTHON, "-c", "pass"])
    dead.wait(timeout=30)
    gone = cs.check_state({"pid": dead.pid})
    assert gone["stale"] and not gone["running"] and gone["reason"] == "process gone"
    assert cs.check_state({"port": 1})["reason"] == "process gone"


def test_terminate_process(tmp_path, children):
    proc = _spawn(tmp_path, "companion_server.py")
    children.append(proc)
    assert _wait_until(lambda: cs.process_alive(proc.pid))
    outcome = cs.terminate_process(proc.pid, timeout=5)
    assert outcome in ("terminated", "killed")
    proc.wait(timeout=10)
    assert not cs.process_alive(proc.pid)
    assert cs.terminate_process(proc.pid) == "already gone"


def test_port_owner_finds_this_process():
    if os.name != "nt" and not (shutil.which("lsof") or shutil.which("ss")):
        pytest.skip("neither lsof nor ss is installed")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        owner = cs.port_owner(port)
    assert owner is not None
    assert owner["pid"] == os.getpid()
    assert "python" in owner["program"].lower()


def test_port_owner_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert cs.port_owner(port) is None


# ---------------------------------------------------------------------------
# The other platforms' branches (monkeypatched)
# ---------------------------------------------------------------------------

def _no_os_kill(*args):
    raise AssertionError("os.kill must not be used on Windows: it terminates the process")


def test_windows_branch_never_calls_os_kill(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs.os, "kill", _no_os_kill)
    monkeypatch.setattr(cs, "_win_process_alive", lambda pid: pid == 42)
    monkeypatch.setattr(cs, "_win_command_line_ntquery",
                        lambda pid: {42: "python.exe companion_server.py --port 8765"}.get(pid))
    monkeypatch.setattr(cs, "_win_command_line_cim", lambda pid: None)
    assert cs.process_alive(42) and not cs.process_alive(43)
    assert cs.is_companion_process(42)
    assert cs.check_state({"pid": 42})["running"]
    assert cs.check_state({"pid": 43})["reason"] == "process gone"


def test_windows_command_line_falls_back_to_cim(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs, "_win_command_line_ntquery", lambda pid: None)
    monkeypatch.setattr(cs, "_win_command_line_cim", lambda pid: "py companion_server.py")
    assert cs.process_command_line(7) == "py companion_server.py"


def test_windows_terminate_uses_terminate_process(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs.os, "kill", _no_os_kill)
    monkeypatch.setattr(cs, "_win_process_alive", lambda pid: True)
    calls = []
    monkeypatch.setattr(cs, "_win_terminate", lambda pid, timeout: calls.append((pid, timeout)) or "terminated")
    assert cs.terminate_process(42, timeout=3) == "terminated"
    assert calls == [(42, 3)]
    monkeypatch.setattr(cs, "_win_process_alive", lambda pid: False)
    assert cs.terminate_process(42) == "already gone"


def test_windows_port_owner_netstat_fallback(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs, "_win_listeners", lambda family: [])
    netstat = (
        "\nActive Connections\n\n  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8765         127.0.0.1:50000        ESTABLISHED     11\n"
        "  TCP    0.0.0.0:18765          0.0.0.0:0              LISTENING       12\n"
        "  TCP    0.0.0.0:8765           0.0.0.0:0              ABHÖREN         4321\n"
    )
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=10: netstat if cmd[0] == "netstat" else None)
    monkeypatch.setattr(cs, "_win_image_name", lambda pid: "C:\\Program Files\\Java\\bin\\java.exe")
    assert cs.port_owner(8765) == {"pid": 4321, "program": "java.exe"}
    assert cs.port_owner(9999) is None


def test_windows_port_owner_from_tcp_table(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(cs, "_win_listeners",
                        lambda family: [(80, 4), (8765, 77)] if family == cs._AF_INET6 else [(80, 4)])
    monkeypatch.setattr(cs, "_win_image_name", lambda pid: None)
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=10: '"node.exe","77","Console","1","10,000 K"\n')
    assert cs.port_owner(8765) == {"pid": 77, "program": "node.exe"}
    assert cs.port_owner(80) == {"pid": 4, "program": "System"}


def _fake_kill(alive: set, signals: list):
    def kill(pid, sig):
        signals.append((pid, sig))
        if pid not in alive:
            raise ProcessLookupError(pid)
        if pid == 99:
            raise PermissionError(pid)
        if sig != 0 and pid in alive:
            alive.discard(pid)
    return kill


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_posix_branch(monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    signals = []
    monkeypatch.setattr(cs.os, "kill", _fake_kill({42, 43, 99}, signals))
    lines = {"42": "/usr/bin/python3 agent/scripts/companion_server.py", "43": "/usr/bin/vim notes.txt"}
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=10: lines.get(cmd[-1]))
    real_open = open

    def no_proc(path, *args, **kwargs):     # /proc/<pid>/cmdline of a fake PID doesn't exist anywhere
        if str(path).startswith("/proc/"):
            raise FileNotFoundError(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr("builtins.open", no_proc)
    assert cs.process_alive(42) and cs.process_alive(99) and not cs.process_alive(44)
    assert cs.is_companion_process(42)
    assert not cs.is_companion_process(43)
    assert cs.check_state({"pid": 43})["reason"] == "pid reused by another program"
    assert signals[0] == (42, 0)


def test_posix_terminate_sigterm_then_sigkill(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(cs, "_reap", lambda pid: None)
    monkeypatch.setattr(cs.time, "sleep", lambda s: None)
    # SIGTERM is ignored; the second signal (SIGKILL) ends it
    sent = []
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)    # Windows has no SIGKILL
    monkeypatch.setattr(cs, "process_alive", lambda pid: len(sent) < 2)
    monkeypatch.setattr(cs.os, "kill", lambda pid, sig: sent.append(sig))
    assert cs.terminate_process(42, timeout=0.2) == "killed"
    assert sent[0] == signal.SIGTERM and sent[-1] == sigkill


def test_posix_terminate_clean_sigterm(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    state = {"alive": True}
    sent = []

    def kill(pid, sig):
        sent.append(sig)
        state["alive"] = False

    monkeypatch.setattr(cs, "process_alive", lambda pid: state["alive"])
    monkeypatch.setattr(cs, "_reap", lambda pid: None)
    monkeypatch.setattr(cs.os, "kill", kill)
    assert cs.terminate_process(42) == "terminated"
    assert sent == [signal.SIGTERM]


def test_posix_port_owner_lsof_and_ss(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    outputs = {"lsof": "p4321\n", "ps": "/usr/bin/node\n"}
    monkeypatch.setattr(cs, "_run", lambda cmd, timeout=10: outputs.get(cmd[0]))
    assert cs.port_owner(8765) == {"pid": 4321, "program": "node"}
    outputs.pop("lsof")
    outputs["ss"] = 'LISTEN 0 5 0.0.0.0:8765 0.0.0.0:* users:(("python3",pid=555,fd=3))\n'
    outputs["ps"] = "python3\n"
    assert cs.port_owner(8765) == {"pid": 555, "program": "python3"}
    outputs.pop("ss")
    assert cs.port_owner(8765) is None


# ---------------------------------------------------------------------------
# Terminal title
# ---------------------------------------------------------------------------

class FakeTty(io.StringIO):
    def isatty(self):
        return True


def test_terminal_title_needs_a_terminal(monkeypatch):
    called = []
    monkeypatch.setattr(cs, "_set_console_title_windows", lambda text: called.append(text) or True)
    stream = io.StringIO()
    assert cs.set_terminal_title("agentic-fm companion :8765 (PID 1)", stream) is False
    assert stream.getvalue() == "" and called == []


def test_terminal_title_osc_escape(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    stream = FakeTty()
    assert cs.set_terminal_title("agentic-fm companion :8765 (PID 1)\x07evil", stream) is True
    assert stream.getvalue() == "\x1b]0;agentic-fm companion :8765 (PID 1) evil\x07"


def test_terminal_title_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    titles = []
    monkeypatch.setattr(cs, "_set_console_title_windows", lambda text: titles.append(text) or True)
    stream = FakeTty()
    assert cs.set_terminal_title("agentic-fm companion :8765 (PID 1)", stream) is True
    assert titles == ["agentic-fm companion :8765 (PID 1)"] and stream.getvalue() == ""
    # No console (SetConsoleTitleW fails): fall back to the escape
    monkeypatch.setattr(cs, "_set_console_title_windows", lambda text: False)
    assert cs.set_terminal_title("t", stream) is True
    assert stream.getvalue() == "\x1b]0;t\x07"
