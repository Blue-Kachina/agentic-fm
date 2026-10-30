#!/usr/bin/env python3
"""
companion_state.py - Where the companion keeps its state, and how to find a running one.

The per-user state folder holds the companion's log file (companion-server.log), the state
file of the running companion (companion.state.json: PID, port, folder, log file, token) and
the conversion records (jobs/). The helpers here also check whether a PID is alive and still
a companion, name the process listening on a port, set the terminal title and stop a process,
on Windows, macOS and Linux/WSL.

Stdlib only (ctypes on Windows, ps/lsof elsewhere), so the companion keeps running without
extra packages. Imports nothing from companion_server: companion_control uses it too.

State folder (COMPANION_STATE_DIR overrides it):
  Windows   %LOCALAPPDATA%\\agentic-fm
  macOS     ~/Library/Logs/agentic-fm
  elsewhere ${XDG_STATE_HOME:-~/.local/state}/agentic-fm
"""

import json
import logging
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import time
import types
from logging.handlers import RotatingFileHandler

STATE_DIR_ENV = "COMPANION_STATE_DIR"      # override, mainly for tests
LOG_FILE_NAME = "companion-server.log"
STATE_FILE_NAME = "companion.state.json"
JOBS_DIR_NAME = "jobs"
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5
TOKEN_HEADER = "X-Companion-Token"
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
COMPANION_SCRIPT = "companion_server.py"

log = logging.getLogger("companion_state")


# ---------------------------------------------------------------------------
# State folder, log file, state file
# ---------------------------------------------------------------------------

def _is_windows() -> bool:
    return sys.platform == "win32"


def _is_mac() -> bool:
    return sys.platform == "darwin"


def _make_private_dir(path: str) -> None:
    """Create path (and parents) readable only by the user: 0o700 on POSIX, the profile's ACL on Windows."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    if not _is_windows():
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            log.debug("Could not make %s private: %s", path, exc)


def state_dir(create: bool = True) -> str:
    """The per-user state folder (see the module docstring); created user-only when create is True."""
    override = os.environ.get(STATE_DIR_ENV, "").strip()
    if override:
        path = os.path.abspath(os.path.expanduser(override))
    elif _is_windows():
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        path = os.path.join(base, "agentic-fm")
    elif _is_mac():
        # Not ~/Library/Application Support/AgenticFM: that is the commercial plug-in's folder
        # (companion_server.APP_SUPPORT_DIR), and its mere existence makes _probe_plugin report
        # the plug-in as installed.
        path = os.path.join(os.path.expanduser("~"), "Library", "Logs", "agentic-fm")
    else:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        path = os.path.join(base, "agentic-fm")
    if create:
        _make_private_dir(path)
    return path


def default_log_file() -> str:
    return os.path.join(state_dir(), LOG_FILE_NAME)


def state_file_path() -> str:
    return os.path.join(state_dir(), STATE_FILE_NAME)


def jobs_dir(create: bool = True) -> str:
    path = os.path.join(state_dir(create), JOBS_DIR_NAME)
    if create:
        _make_private_dir(path)
    return path


class _CompanionFileHandler(RotatingFileHandler):
    """RotatingFileHandler whose rollover failure is not fatal.

    On Windows the rename fails while another process holds the file open without
    FILE_SHARE_DELETE (an editor, or `attach` between two reads). The handler then keeps
    writing to the current file and tries again a minute later.
    """

    _RETRY_SECONDS = 60.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._retry_after = 0.0

    def shouldRollover(self, record):
        if time.monotonic() < self._retry_after:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self._retry_after = time.monotonic() + self._RETRY_SECONDS
            if self.stream is None:
                self.stream = self._open()


def make_file_handler(path: str) -> logging.Handler:
    """A size-rotating UTF-8 file handler at debug level for the companion's log file."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    handler = _CompanionFileHandler(path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
                                    encoding="utf-8", delay=False)
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    return handler


def new_token() -> str:
    return secrets.token_urlsafe(32)


def write_state(fields: dict, path: "str | None" = None) -> str:
    """Write the state file atomically (temp file + os.replace), mode 0o600 on POSIX. Returns its path."""
    path = path or state_file_path()
    folder = os.path.dirname(os.path.abspath(path))
    _make_private_dir(folder)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".companion.state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(fields, f, indent=2, ensure_ascii=False)
            f.write("\n")
        if not _is_windows():
            os.chmod(tmp, 0o600)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: a reader has the file open right now
                if attempt == 4:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    return path


def read_state(path: "str | None" = None) -> "dict | None":
    """The state file as a dict, or None when it is missing, unreadable or malformed."""
    path = path or os.path.join(state_dir(create=False), STATE_FILE_NAME)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_state(path: "str | None" = None, pid: "int | None" = None) -> bool:
    """Remove the state file; with pid, only when the file is that process's (never a newer companion's)."""
    path = path or os.path.join(state_dir(create=False), STATE_FILE_NAME)
    if pid is not None:
        state = read_state(path)
        if state is None or state.get("pid") != pid:
            return False
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not remove the state file %s: %s", path, exc)
        return False
    return True


# ---------------------------------------------------------------------------
# Windows API (ctypes), loaded on first use
# ---------------------------------------------------------------------------

_PROCESS_TERMINATE = 0x0001
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_ERROR_INSUFFICIENT_BUFFER = 122
_PROCESS_COMMAND_LINE_INFORMATION = 60     # NtQueryInformationProcess class, Windows 8.1+
_TCP_TABLE_OWNER_PID_LISTENER = 3
_AF_INET = 2
_AF_INET6 = 23

_win_api = None


def _win():
    """kernel32/ntdll/iphlpapi with argtypes set, on private WinDLL objects (ctypes.windll stays untouched)."""
    global _win_api
    if _win_api is None:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                   ctypes.POINTER(wintypes.DWORD)]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        k32.SetConsoleTitleW.argtypes = [wintypes.LPCWSTR]
        k32.SetConsoleTitleW.restype = wintypes.BOOL

        ntdll = ctypes.WinDLL("ntdll")
        ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                    wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long

        iphlpapi = ctypes.WinDLL("iphlpapi")
        iphlpapi.GetExtendedTcpTable.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                                                 wintypes.ULONG, ctypes.c_int, wintypes.ULONG]
        iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD

        class UnicodeString(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        _win_api = types.SimpleNamespace(ctypes=ctypes, wintypes=wintypes, kernel32=k32, ntdll=ntdll,
                                         iphlpapi=iphlpapi, UnicodeString=UnicodeString)
    return _win_api


def _win_open(pid: int, access: int):
    return _win().kernel32.OpenProcess(access, False, pid)


def _win_process_alive(pid: int) -> bool:
    api = _win()
    handle = _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        # Access denied means the process exists (another user's, or protected)
        return api.ctypes.get_last_error() == _ERROR_ACCESS_DENIED
    try:
        code = api.wintypes.DWORD()
        if not api.kernel32.GetExitCodeProcess(handle, api.ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        api.kernel32.CloseHandle(handle)


def _win_command_line_ntquery(pid: int) -> "str | None":
    api = _win()
    ctypes, wintypes = api.ctypes, api.wintypes
    handle = _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        size = wintypes.ULONG(0)
        api.ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION, None, 0,
                                            ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        status = api.ntdll.NtQueryInformationProcess(handle, _PROCESS_COMMAND_LINE_INFORMATION, buf,
                                                     size, ctypes.byref(size))
        if status != 0:
            return None
        text = api.UnicodeString.from_buffer(buf)
        if not text.Buffer or not text.Length:
            return ""
        return ctypes.wstring_at(text.Buffer, text.Length // 2)
    finally:
        api.kernel32.CloseHandle(handle)


def _win_command_line_cim(pid: int) -> "str | None":
    """Fallback through PowerShell CIM (slow: about a second)."""
    script = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], timeout=15)
    if out is None:
        return None
    out = out.strip()
    return out or None


def _win_image_name(pid: int) -> "str | None":
    api = _win()
    handle = _win_open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        return None
    try:
        size = api.wintypes.DWORD(32768)
        buf = api.ctypes.create_unicode_buffer(size.value)
        if not api.kernel32.QueryFullProcessImageNameW(handle, 0, buf, api.ctypes.byref(size)):
            return None
        return buf.value
    finally:
        api.kernel32.CloseHandle(handle)


def _win_listeners(family: int) -> list:
    """[(port, pid)] of the TCP listeners of one address family, from GetExtendedTcpTable."""
    import struct
    api = _win()
    ctypes, wintypes = api.ctypes, api.wintypes
    size = wintypes.DWORD(0)
    buf = None
    for _ in range(5):
        buf = ctypes.create_string_buffer(max(size.value, 4))
        ret = api.iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, family,
                                               _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if ret == 0:
            break
        if ret != _ERROR_INSUFFICIENT_BUFFER:
            return []
    else:
        return []
    raw = buf.raw
    count = struct.unpack_from("<I", raw, 0)[0]
    # MIB_TCPROW_OWNER_PID: state, local addr, local port, remote addr, remote port, pid (6 DWORDs).
    # MIB_TCP6ROW_OWNER_PID: local addr[16], scope, local port, remote addr[16], scope, remote port, state, pid.
    row_size, port_offset, pid_offset = (24, 8, 20) if family == _AF_INET else (56, 20, 52)
    rows = []
    for i in range(count):
        start = 4 + i * row_size
        if start + row_size > len(raw):
            break
        port = struct.unpack_from(">H", raw, start + port_offset)[0]   # network byte order
        pid = struct.unpack_from("<I", raw, start + pid_offset)[0]
        rows.append((port, pid))
    return rows


def _win_port_owner_netstat(port: int) -> "int | None":
    """Fallback: parse `netstat -ano` (the state column is localized, so match listeners by the remote address)."""
    out = _run(["netstat", "-ano", "-p", "TCP"], timeout=15) or ""
    out += _run(["netstat", "-ano", "-p", "TCPv6"], timeout=15) or ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[1].endswith(f":{port}") \
                and parts[2] in ("0.0.0.0:0", "[::]:0") and parts[-1].isdigit():
            return int(parts[-1])
    return None


def _set_console_title_windows(text: str) -> bool:
    try:
        return bool(_win().kernel32.SetConsoleTitleW(text))
    except (OSError, AttributeError):
        return False


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------

def _run(cmd: list, timeout: float = 10) -> "str | None":
    """stdout of cmd, or None when it is missing, fails or times out."""
    kwargs = {}
    if _is_windows():
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, **kwargs)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def process_alive(pid: int) -> bool:
    """Whether a process with this PID exists. Never os.kill(pid, 0) on Windows: that kills it."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if _is_windows():
        try:
            return _win_process_alive(pid)
        except OSError:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    except OSError:
        return False
    return True


def process_command_line(pid: int) -> "str | None":
    """The process's command line, or None when it can't be read (gone, or not ours to read)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if _is_windows():
        try:
            line = _win_command_line_ntquery(pid)
        except OSError:
            line = None
        return line if line is not None else _win_command_line_cim(pid)
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            if raw:
                return raw.rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            pass
    out = _run(["ps", "-ww", "-o", "command=", "-p", str(pid)])
    if out is None:
        return None
    out = out.strip()
    return out or None


def process_program(pid: int) -> "str | None":
    """The program's name (python.exe, Python, java...) for a PID, or None."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if _is_windows():
        if pid == 4:
            return "System"
        try:
            image = _win_image_name(pid)
        except OSError:
            image = None
        if image:
            return os.path.basename(image)
        out = _run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        match = re.match(r'\s*"([^"]+)"', out or "")
        return match.group(1) if match else None
    out = _run(["ps", "-o", "comm=", "-p", str(pid)])
    if not out or not out.strip():
        return None
    return os.path.basename(out.strip())


def is_companion_process(pid: int) -> bool:
    """Alive, and its command line runs companion_server.py."""
    if not process_alive(pid):
        return False
    line = process_command_line(pid)
    return bool(line) and COMPANION_SCRIPT in line


def check_state(state: "dict | None" = None) -> dict:
    """Whether the companion in the state file (read when state is None) is still running.

    Returns {"state", "running", "stale", "reason"}; reason is "no state file", "running",
    "process gone" or "pid reused by another program".
    """
    if state is None:
        state = read_state()
    if state is None:
        return {"state": None, "running": False, "stale": False, "reason": "no state file"}
    pid = state.get("pid")
    if not isinstance(pid, int) or not process_alive(pid):
        return {"state": state, "running": False, "stale": True, "reason": "process gone"}
    if not is_companion_process(pid):
        return {"state": state, "running": False, "stale": True, "reason": "pid reused by another program"}
    return {"state": state, "running": True, "stale": False, "reason": "running"}


def port_owner(port: int) -> "dict | None":
    """{"pid", "program"} of the process listening on TCP port, or None when none is found."""
    pid = None
    if _is_windows():
        try:
            for family in (_AF_INET, _AF_INET6):
                pid = next((p for lp, p in _win_listeners(family) if lp == port), None)
                if pid is not None:
                    break
        except OSError:
            pid = None
        if pid is None:
            pid = _win_port_owner_netstat(port)
    else:
        out = _run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fp"])
        if out:
            match = re.search(r"^p(\d+)", out, re.MULTILINE)
            pid = int(match.group(1)) if match else None
        if pid is None and sys.platform.startswith("linux"):
            out = _run(["ss", "-ltnpH", f"sport = :{port}"])
            match = re.search(r"pid=(\d+)", out or "")
            pid = int(match.group(1)) if match else None
    if pid is None:
        return None
    return {"pid": pid, "program": process_program(pid) or "unknown program"}


def terminate_process(pid: int, timeout: float = 5.0) -> str:
    """Stop a process the caller has checked is a companion; say what happened.

    Windows: TerminateProcess. POSIX: SIGTERM, wait up to timeout, then SIGKILL.
    Returns "terminated", "killed", "already gone", or a short failure description.
    """
    pid = int(pid)
    if not process_alive(pid):
        return "already gone"
    if _is_windows():
        return _win_terminate(pid, timeout)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return "already gone"
    except PermissionError:
        return "not allowed to stop it (another user's process)"
    if _wait_gone(pid, timeout):
        return "terminated"
    try:
        os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
    except ProcessLookupError:
        return "terminated"
    _wait_gone(pid, 2.0)
    return "killed"


def _reap(pid: int) -> None:
    """Collect a child's exit status, so a stopped child doesn't stay a zombie that looks alive."""
    waitpid = getattr(os, "waitpid", None)
    if waitpid is None or _is_windows():
        return
    try:
        waitpid(pid, getattr(os, "WNOHANG", 1))
    except (ChildProcessError, OSError):
        pass


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        _reap(pid)
        if not process_alive(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)


def _win_terminate(pid: int, timeout: float) -> str:
    api = _win()
    handle = _win_open(pid, _PROCESS_TERMINATE | _SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION)
    if not handle:
        if not process_alive(pid):
            return "already gone"
        return f"could not open the process (error {api.ctypes.get_last_error()})"
    try:
        if not api.kernel32.TerminateProcess(handle, 1):
            error = api.ctypes.get_last_error()
            if not process_alive(pid):
                return "already gone"
            return f"TerminateProcess failed (error {error})"
        api.kernel32.WaitForSingleObject(handle, max(0, int(timeout * 1000)))
        return "terminated"
    finally:
        api.kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# Terminal title
# ---------------------------------------------------------------------------

def set_terminal_title(text: str, stream=None) -> bool:
    """Set the terminal's title when stream (default sys.stdout) is a terminal. Returns whether it did."""
    stream = sys.stdout if stream is None else stream
    try:
        if stream is None or not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text))
    if _is_windows() and _set_console_title_windows(text):
        return True
    try:
        stream.write(f"\x1b]0;{text}\x07")
        stream.flush()
    except (OSError, ValueError):
        return False
    return True
