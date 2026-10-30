#!/usr/bin/env python3
"""
companion_control.py - find, watch and stop a running companion server (client side).

companion_server.py dispatches its `status`, `stop`, `restart` and `attach` subcommands
here, and calls handle_port_clash() when its port is already taken at start. Everything
goes through the running companion's GET /health and POST /shutdown, the per-user state
file and the companion's log file (companion_state). Nothing here imports companion_server.

Exit codes: 0 done (or nothing was running, for stop), 1 failed or not running,
2 stop refused because a conversion is running (use --force, or wait for it).
"""

import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request

import companion_state

log = logging.getLogger("companion_control")

AFM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# How often attach checks that the companion is still there (seconds)
ATTACH_HEALTH_EVERY = 2.0
# How much of the end of the log file to read for the first lines
_TAIL_BYTES = 256 * 1024


def _out(out):
    return out if out is not None else sys.stdout


def _say(out, text: str = "") -> None:
    out = _out(out)
    out.write(text + "\n")
    try:
        out.flush()
    except (OSError, ValueError):
        pass


def _same_folder(a: "str | None", b: "str | None") -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _base_url(host: str, port: int) -> str:
    # A wildcard bind is reached on loopback
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host and not host.startswith("["):
        host = "[%s]" % host
    return "http://%s:%d" % (host, port)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def fetch_health(host: str, port: int, timeout: float = 2.0) -> "dict | None":
    """GET /health. None when nothing answers or what answers is not a companion."""
    try:
        with urllib.request.urlopen(_base_url(host, port) + "/health", timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if isinstance(body, dict) and body.get("status") == "ok" and "version" in body:
        return body
    return None


def request_shutdown(host: str, port: int, token: str, *, force: bool = False, wait: bool = False,
                     timeout: float = 5.0) -> tuple:
    """POST /shutdown with the state file's token. Returns (HTTP status or None, body)."""
    data = json.dumps({"force": bool(force), "wait": bool(wait)}).encode("utf-8")
    req = urllib.request.Request(
        _base_url(host, port) + "/shutdown", data=data, method="POST",
        headers={"Content-Type": "application/json", companion_state.TOKEN_HEADER: token or ""},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, _json_body(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except OSError:
            raw = b""
        return exc.code, _json_body(raw)
    except (OSError, urllib.error.URLError) as exc:
        return None, {"error": str(getattr(exc, "reason", exc))}


def _json_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {"error": raw.decode("utf-8", "replace")[:200]}
    return body if isinstance(body, dict) else {"body": body}


# ---------------------------------------------------------------------------
# Describing a companion
# ---------------------------------------------------------------------------

def _uptime(seconds) -> str:
    try:
        seconds = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh %02dm" % (hours, minutes)
    if minutes:
        return "%dm %02ds" % (minutes, secs)
    return "%ds" % secs


def _job_text(job) -> str:
    if not job:
        return "none"
    if not isinstance(job, dict):
        return str(job)
    name = job.get("solution_name") or "?"
    parts = ["job %s" % job.get("job_id", "?"), job.get("state") or "?"]
    progress = job.get("progress")
    if isinstance(progress, dict) and progress.get("message"):
        parts.append(str(progress["message"]))
    return "%s (%s)" % (name, ", ".join(parts))


def describe(health: "dict | None", state: "dict | None") -> str:
    """Who the companion is: PID, start time, folder, log file, how it was launched, conversion.

    /health wins over the state file; the state file fills in what an older companion's
    /health doesn't say.
    """
    info = dict(state or {})
    info.update({k: v for k, v in (health or {}).items() if v is not None})
    rows = []

    def row(label, value):
        if value not in (None, ""):
            rows.append("  %-12s%s" % (label, value))

    row("PID", info.get("pid"))
    started = info.get("started_at") or ""
    up = _uptime(info.get("uptime_seconds"))
    row("Started", "%s (up %s)" % (started, up) if started and up else started)
    if info.get("host") or info.get("port"):
        row("Address", "%s:%s" % (info.get("host", "?"), info.get("port", "?")))
    row("Folder", info.get("afm_root"))
    if "log_file" in info:
        row("Log file", info.get("log_file") or "none (started with --no-log-file)")
    row("Launched by", info.get("launched_by"))
    row("Python", info.get("python"))
    row("Version", info.get("version"))
    if health is not None:
        row("Conversion", _job_text(health.get("conversion")))
        for viewer in health.get("viewers") or []:
            if isinstance(viewer, dict):
                row("Viewer", "%s (%s)" % (viewer.get("url"), viewer.get("output_path")))
    return "\n".join(rows) if rows else "  (no details: it is an older companion)"


def _matching_state(health: "dict | None", state: "dict | None", port: int) -> "dict | None":
    """The state file, when it belongs to the companion on this port."""
    if not state:
        return None
    if health is not None and health.get("pid") is not None:
        return state if state.get("pid") == health.get("pid") else None
    return state if state.get("port") in (None, port) else None


def _disagreement(health: "dict | None", check: dict, port: int) -> "list[str]":
    """What the state file and the port say differently (P3-21 AC7)."""
    notes = []
    state = check.get("state")
    path = companion_state.state_file_path()
    if health is not None and health.get("afm_root") and not _same_folder(health["afm_root"], AFM_ROOT):
        notes.append("It is a companion from another folder: %s (this one is %s)."
                     % (health["afm_root"], AFM_ROOT))
    if check.get("stale"):
        notes.append("The state file %s is stale (%s, PID %s)."
                     % (path, check.get("reason"), (state or {}).get("pid")))
    elif state and health is not None and health.get("pid") is not None and state.get("pid") != health.get("pid"):
        notes.append("The state file %s names another companion (PID %s on port %s)."
                     % (path, state.get("pid"), state.get("port")))
    elif state and health is None and check.get("running") and state.get("port") not in (None, port):
        notes.append("The state file %s names a companion on port %s (PID %s), not on %d."
                     % (path, state.get("port"), state.get("pid"), port))
    elif not state and health is not None:
        notes.append("There is no state file for it (%s): it may use another state folder." % path)
    return notes


def _remove_stale(check: dict, out) -> None:
    state = check.get("state") or {}
    if check.get("stale") and companion_state.remove_state(pid=state.get("pid")):
        _say(out, "Removed the stale state file %s (%s, PID %s)."
             % (companion_state.state_file_path(), check.get("reason"), state.get("pid")))


# ---------------------------------------------------------------------------
# status / stop
# ---------------------------------------------------------------------------

def cmd_status(host: str, port: int, out=None) -> int:
    """Print who is running on host:port. 0 when a companion is running, 1 when not."""
    health = fetch_health(host, port)
    check = companion_state.check_state()
    state = check.get("state")
    if health is not None:
        _say(out, "A companion is running on %s:%d." % (host, port))
        _say(out, describe(health, _matching_state(health, state, port)))
        for note in _disagreement(health, check, port):
            _say(out, note)
        return 0
    if check.get("running"):
        _say(out, "A companion is running (PID %s) but doesn't answer on %s:%d."
             % (state.get("pid"), host, port))
        _say(out, describe(None, state))
        for note in _disagreement(None, check, port):
            _say(out, note)
        return 0
    _say(out, "No companion running on %s:%d." % (host, port))
    if check.get("stale"):
        for note in _disagreement(None, check, port):
            _say(out, note)
    return 1


def _wait_gone(host: str, port: int, pid: "int | None", timeout: float) -> bool:
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        answering = fetch_health(host, port, timeout=0.5)
        if answering is not None and pid is not None and answering.get("pid") not in (None, pid):
            answering = None    # another companion took the port: ours is gone from it
        alive = pid is not None and companion_state.process_alive(pid)
        if answering is None and not alive:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _terminate(pid: int, out) -> bool:
    """Kill pid, but only while it is still a companion. True when it is gone."""
    if not companion_state.is_companion_process(pid):
        if companion_state.process_alive(pid):
            _say(out, "PID %d is no longer a companion server: not killing it." % pid)
            return False
        return True
    result = companion_state.terminate_process(pid)
    _say(out, "PID %d: %s." % (pid, result))
    companion_state.remove_state(pid=pid)
    return not companion_state.process_alive(pid)


def cmd_stop(host: str, port: int, *, timeout: float = 15.0, force: bool = False, out=None) -> int:
    """Stop the companion on host:port: POST /shutdown, wait, then kill it if it is still there.

    0 stopped (or nothing was running), 1 failed, 2 refused because a conversion is running.
    """
    health = fetch_health(host, port)
    check = companion_state.check_state()
    state = _matching_state(health, check.get("state"), port)
    pid = (health or {}).get("pid") or (state or {}).get("pid")

    if health is None:
        if check.get("running") and state is not None:
            # Alive and a companion, but it doesn't answer HTTP: nothing clean left to try
            _say(out, "The companion (PID %s) doesn't answer on %s:%d; terminating it." % (pid, host, port))
            return 0 if _terminate(pid, out) else 1
        _remove_stale(check, out)
        if check.get("running"):
            _say(out, "No companion on %s:%d (the one in the state file, PID %s, is on port %s)."
                 % (host, port, check["state"].get("pid"), check["state"].get("port")))
        else:
            _say(out, "No companion running on %s:%d." % (host, port))
        return 0

    _say(out, "Stopping the companion on %s:%d (PID %s)." % (host, port, pid if pid is not None else "?"))
    token = (state or {}).get("token")
    if token:
        status, body = request_shutdown(host, port, token, force=force)
    else:
        status, body = None, {"error": "no state file token for it"}

    if status == 409:
        _say(out, "It is converting: %s." % _job_text(body.get("job")))
        _say(out, "Not stopped. Wait for the conversion, or stop it anyway with --force.")
        return 2
    if status == 202:
        _say(out, "Asked it to stop (%s)." % body.get("status", "stopping"))
    else:
        reason = body.get("error") or ("HTTP %s" % status if status else "no answer")
        _say(out, "POST /shutdown failed: %s." % reason)
        conversion = health.get("conversion")
        if conversion and not force:
            _say(out, "It is converting: %s." % _job_text(conversion))
            _say(out, "Not killing it mid-conversion. Use --force to stop it anyway.")
            return 2
        timeout = 0    # nothing asked it to stop: go straight to the PID

    if _wait_gone(host, port, pid, timeout):
        _say(out, "Stopped.")
        return 0
    if pid is None:
        _say(out, "It is still running and didn't say its PID; stop it by hand.")
        return 1
    if timeout:
        _say(out, "Still running after %gs; terminating PID %s." % (timeout, pid))
    else:
        _say(out, "Terminating PID %s." % pid)
    if _terminate(pid, out) and _wait_gone(host, port, pid, 2.0):
        _say(out, "Stopped.")
        return 0
    _say(out, "Could not stop PID %s." % pid)
    return 1


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------

def _file_id(st) -> tuple:
    return (st.st_dev, st.st_ino)


def _read_from(path: str, pos: int) -> bytes:
    # Open, read, close: holding the file open would block RotatingFileHandler's rename on Windows
    with open(path, "rb") as fh:
        fh.seek(pos)
        return fh.read()


def _tail_bytes(path: str, lines: int) -> tuple:
    """(the last `lines` complete lines as bytes, the size read up to)."""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        start = max(0, size - _TAIL_BYTES)
        fh.seek(start)
        data = fh.read(size - start)
    newline = data.rfind(b"\n")
    end = start + newline + 1 if newline >= 0 else start    # a partial last line is read later
    if lines <= 0:
        return b"", end
    complete = data[: newline + 1]
    if start > 0:
        complete = complete[complete.find(b"\n") + 1:]     # drop the cut first line
    return b"".join(complete.splitlines(keepends=True)[-lines:]), end


def follow_log(path: str, out, stop_event: threading.Event, *, lines: int = 40, poll: float = 0.25) -> None:
    """Print the last `lines` of path, then new lines as they come, until stop_event is set.

    Survives rotation (the file renamed away and a new one made) and truncation: it finishes
    the rotated file's last lines, then reads the new one from the start. A missing file is
    waited for.
    """
    out = _out(out)
    pending = b""
    pos = 0
    ident = None
    waiting_said = False

    def emit(data: bytes):
        nonlocal pending
        pending += data
        cut = pending.rfind(b"\n")
        if cut < 0:
            return
        # The handler writes CRLF on Windows; out adds its own line endings
        text = pending[: cut + 1].decode("utf-8", "replace").replace("\r\n", "\n")
        pending = pending[cut + 1:]
        out.write(text)
        try:
            out.flush()
        except (OSError, ValueError):
            pass

    try:
        st = os.stat(path)
        first, pos = _tail_bytes(path, lines)
        ident = _file_id(st)
        emit(first)
    except OSError:
        pass

    while not stop_event.is_set():
        try:
            st = os.stat(path)
        except OSError:
            if not waiting_said:
                _say(out, "(waiting for %s)" % path)
                waiting_said = True
            stop_event.wait(poll)
            continue
        waiting_said = False
        current = _file_id(st)
        if ident is not None and current != ident:
            # Rotated: the old file is now <path>.1; finish it before starting the new one
            rotated = path + ".1"
            try:
                if _file_id(os.stat(rotated)) == ident:
                    emit(_read_from(rotated, pos))
            except OSError:
                pass
            if pending:
                emit(b"\n")
            pos = 0
        elif st.st_size < pos:
            pending = b""
            pos = 0    # truncated in place
        ident = current
        if st.st_size > pos:
            try:
                data = _read_from(path, pos)
            except OSError:
                data = b""
            pos += len(data)
            emit(data)
        stop_event.wait(poll)


def _attach_wait(stop_event: threading.Event, seconds: float) -> bool:
    """Wait in the main thread so Ctrl-C lands there. Tests replace it to send a Ctrl-C."""
    return stop_event.wait(seconds)


def _ask_detach_or_stop(out, stdin) -> str:
    """'detach' or 'stop'. A second Ctrl-C, end of input or an empty answer means detach."""
    stdin = stdin if stdin is not None else sys.stdin
    for _ in range(3):
        _out(out).write("\n[d]etach (the companion keeps running) or [s]top it? [d/s] ")
        _out(out).flush()
        try:
            answer = stdin.readline()
        except (KeyboardInterrupt, EOFError):
            return "detach"
        if not answer:
            return "detach"
        answer = answer.strip().lower()
        if answer in ("", "d", "detach"):
            return "detach"
        if answer in ("s", "stop"):
            return "stop"
    return "detach"


def cmd_attach(host: str, port: int, *, lines: int = 40, out=None, stdin=None) -> int:
    """Show who is running, then follow its log file. Ctrl-C asks detach or stop."""
    health = fetch_health(host, port)
    check = companion_state.check_state()
    if health is None:
        if check.get("running") and check["state"].get("port") in (None, port):
            _say(out, "The companion (PID %s) doesn't answer on %s:%d. Stop it with: companion_server.py stop"
                 % (check["state"].get("pid"), host, port))
        else:
            _say(out, "No companion running on %s:%d." % (host, port))
        return 1
    state = _matching_state(health, check.get("state"), port)
    log_file = health.get("log_file") or (state or {}).get("log_file")
    _say(out, "Attached to the companion on %s:%d:" % (host, port))
    _say(out, describe(health, state))
    if not log_file:
        _say(out, "It writes no log file, so there is nothing to follow.")
        return 1
    _say(out, "Following %s. Press Ctrl-C to detach or stop it." % log_file)
    _say(out, "")

    pid = health.get("pid")
    stop_event = threading.Event()
    follower = threading.Thread(target=follow_log, args=(log_file, _out(out), stop_event),
                                kwargs={"lines": lines}, daemon=True)
    follower.start()
    try:
        while True:
            if _attach_wait(stop_event, ATTACH_HEALTH_EVERY):
                return 0
            now = fetch_health(host, port, timeout=1.0)
            if now is None or (pid is not None and now.get("pid") not in (None, pid)):
                stop_event.set()
                follower.join(1.0)
                _say(out, "")
                _say(out, "The companion stopped.")
                return 0
    except KeyboardInterrupt:
        stop_event.set()
        follower.join(1.0)
    choice = _ask_detach_or_stop(out, stdin)
    if choice == "stop":
        return cmd_stop(host, port, out=out)
    _say(out, "Detached. The companion keeps running%s; stop it with: companion_server.py stop"
         % (" (PID %s)" % pid if pid is not None else ""))
    return 0


# ---------------------------------------------------------------------------
# Port clash at start
# ---------------------------------------------------------------------------

def _netstat_hint(port: int) -> str:
    if sys.platform == "win32":
        return "netstat -ano | findstr :%d, then Stop-Process -Id <PID>" % port
    return "lsof -i :%d, then kill <PID>" % port


def handle_port_clash(host: str, port: int, *, replace: bool, no_attach: bool,
                      is_tty: "bool | None" = None, out=None) -> "int | None":
    """What to do when the port is taken at start. An exit code, or None: the port is free, start.

    A companion on the port: --replace stops it (None), --no-attach or no terminal exits 1,
    else attach. Anything else holding the port is named (PID and program), exit 1.
    """
    health = fetch_health(host, port)
    check = companion_state.check_state()
    state = check.get("state")

    if health is not None:
        _say(out, "Port %d on %s is already in use by a companion server:" % (port, host))
        _say(out, describe(health, _matching_state(health, state, port)))
        for note in _disagreement(health, check, port):
            _say(out, note)
        _remove_stale(check, out)
        if replace:
            _say(out, "--replace: stopping it and starting here instead.")
            code = cmd_stop(host, port, out=out)
            return None if code == 0 else code
        if no_attach:
            _say(out, "Not attaching (--no-attach). Stop it with: companion_server.py stop")
            return 1
        if is_tty is None:
            is_tty = _isatty(sys.stdin) and _isatty(_out(out))
        if not is_tty:
            _say(out, "No terminal to attach to. Stop it with: companion_server.py stop, "
                      "or start with --replace.")
            return 1
        return cmd_attach(host, port, out=out)

    owner = companion_state.port_owner(port)
    owner_pid = (owner or {}).get("pid")
    if check.get("running") and state and owner_pid is not None and owner_pid == state.get("pid"):
        _say(out, "Port %d on %s is held by the companion in the state file (PID %s), but it doesn't answer."
             % (port, host, owner_pid))
        _say(out, describe(None, state))
        if replace:
            _say(out, "--replace: stopping it and starting here instead.")
            code = cmd_stop(host, port, out=out)
            return None if code == 0 else code
        _say(out, "Stop it with: companion_server.py stop, or start with --replace.")
        return 1

    if owner:
        _say(out, "Port %d on %s is already in use by %s (PID %s), not a companion server. "
                  "Stop that program, or start this one with --port."
             % (port, host, owner.get("program") or "an unknown program", owner_pid))
    else:
        _say(out, "Port %d on %s is already in use by a program that doesn't answer as a companion, "
                  "and its PID could not be found. Stop it (find it with %s), or start this one with --port."
             % (port, host, _netstat_hint(port)))
    for note in _disagreement(None, check, port):
        _say(out, note)
    _remove_stale(check, out)
    return 1


def _isatty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False
