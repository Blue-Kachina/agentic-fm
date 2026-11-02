#!/usr/bin/env python3
"""
companion_server.py - Lightweight HTTP companion server for agentic-fm.

Lightweight HTTP companion server for shell command execution. FileMaker
calls this server via the native Insert from URL step (curl-compatible).

Usage:
    Start server:
        python agent/scripts/companion_server.py

    Start on custom port:
        python agent/scripts/companion_server.py --port 9000

    Find, follow and stop a running companion (from any terminal):
        python agent/scripts/companion_server.py status|attach|stop|restart

    A start while a companion already runs on the port shows it and follows its log
    (--no-attach: show it and exit 1; --replace: stop it and start here). The companion
    logs to companion-server.log in the per-user state folder (--log-file, --no-log-file).

    FileMaker calls it via Insert from URL, e.g. to convert a Save a Copy as XML
    export with fm-saxml:
        POST http://localhost:8765/convert
        GET  http://localhost:8765/convert/<job id>

POST /convert  (body: JSON object)
    solution_name  required  Solution name; the docs go to <docs root>/<solution_name>.
                             For a multi-file export, one name for the whole batch.
    export_path    required  The export folder FileMaker wrote (or one .xml file).
    docs_root      optional  Folder holding every converted solution. Without it:
                             $FM_SAXML_DOCS_ROOT in the companion's environment, else
                             the DIS harness's exports/ folder when agentic-fm is its
                             tools/agentic-fm submodule (created if missing), else 400.
                             The startup log names the default and where it came from.
  202 → {"success": true, "job_id": "3f9c0a1b2c4d", "state": "queued",
         "status_url": "/convert/3f9c0a1b2c4d"}
  400 → {"success": false, "error": "..."}            (bad body, path, or no docs root)
  409 → {"success": false, "error": "...", "job_id": "..."}  (that solution is converting)

GET /convert/<job id>
  200 → {"success": true, "job_id", "state": "queued|running|succeeded|failed",
         "done": false|true, "solution_name", "export_path", "docs_root", "output_path",
         "created_at", "started_at", "finished_at", "exit_code", "error",
         and once succeeded: "kind": "single|batch",
         "counts": {"files", "tables", "fields", "table_occurrences", "relationships",
                    "layouts", "scripts", "custom_functions", "value_lists"},
         "files": [{"name", "counts": {...}, "warning_count"}],
         "warning_count", "warnings": [{"file", "code", "entity", "message"}],
         and once failed: "log_tail" (the end of fm-saxml's output)}
  404 → {"success": false, "error": "No such job: ..."}
  Poll until "done" is true, then check "state".

POST /explode is a deprecated alias of POST /convert for one release: it takes
the old body (export_file_path for export_path) and adds "deprecated": true.

fm-saxml runs as `sys.executable -m fm_saxml build`, so start the companion with
the Python that has fm-saxml-converter installed (the DIS harness .venv). The
launchers in agent/scripts/launchers/ find it: start_companion.cmd / .ps1 on
Windows, start_companion.sh on macOS.
"""

import argparse
import errno
import hmac
import json
import logging
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

# Siblings in agent/scripts, stdlib only: the state folder, log file and state file (companion_state),
# and the client side of status/stop/attach and the port clash (companion_control).
import companion_control
import companion_state

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Built-in defaults — the bottom of the resolution chain, used when neither a
# CLI flag, an env var, nor companion.json supplies a value (so the server always
# boots). See "Resolution precedence" in agent/docs/COMPANION_SERVER.md.
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_ADVERTISE_HOST = "local.hub"
# Idle auto-shutdown: seconds with no requests before the server exits.
# 0 disables it (always-on) — the default, so existing behaviour is unchanged.
DEFAULT_IDLE_TIMEOUT = 0
REMOTE_VERSION_URL = "https://raw.githubusercontent.com/petrowsky/agentic-fm/main/version.txt"

# Canonical companion config (agent/config/companion.json). Optional and gitignored;
# absent/invalid → built-in defaults. It is the single source of truth for the
# server's bind host/port and the address clients use to reach it.
COMPANION_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "config", "companion.json"
)

# Read version from version.txt at the repo root
def _read_local_version() -> str:
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        version_file = os.path.join(here, "..", "..", "version.txt")
        with open(version_file, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return "unknown"

VERSION = _read_local_version()

# ---------------------------------------------------------------------------
# Path helpers (WSL2 / Windows compatibility)
# ---------------------------------------------------------------------------

def _wsl_translate_path(path: str) -> str:
    """Translate FileMaker's Windows-POSIX paths (/C:/...) to WSL2 mount points (/mnt/c/...).

    FileMaker on Windows uses ConvertFromFileMakerPath(...; 1) which produces paths
    like /C:/Users/... instead of the WSL2 equivalent /mnt/c/Users/...
    """
    m = re.match(r"^/([A-Za-z]):(.*)", path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2)
        return f"/mnt/{drive}{rest}"
    return path


def _is_wsl() -> bool:
    """True when the companion runs inside WSL (a Linux kernel built by Microsoft)."""
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    return "microsoft" in platform.uname().release.lower()


def _is_native_windows() -> bool:
    """True when the companion runs on Windows itself (not WSL)."""
    return sys.platform == "win32"


def _translate_incoming_path(path: str) -> str:
    """Map a path FileMaker sent to one this process can open.

    Only WSL needs a translation (/C:/... → /mnt/c/...). On native Windows and
    macOS the path FileMaker sends is used unchanged — drive paths
    (C:\\Users\\...) and UNC paths (\\\\server\\share\\...) included.
    """
    if _is_wsl():
        return _wsl_translate_path(path)
    return path


def _load_automation_config() -> dict:
    """Load agent/config/automation.json relative to this script, if present."""
    here = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(here, "..", "config", "automation.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


_automation_config: dict = _load_automation_config()


def _resolve_export_path_for_wsl(solution_name: str) -> str:
    """Resolve the FM export file path when FileMaker sends '?' on WSL2.

    FileMaker on Windows exports via Get(DesktopPath) & Get(FileName) & ".xml".
    When ConvertFromFileMakerPath fails (returns '?'), we reconstruct the path
    by querying Windows for the actual Desktop shell folder via PowerShell
    (handles OneDrive-redirected Desktops), then converting with wslpath.

    Falls back to export_dir_override in automation.json if auto-detection fails.
    """
    # Config override takes precedence — skip auto-detection entirely
    export_dir = _automation_config.get("export_dir_override", "")
    if export_dir:
        path = os.path.join(os.path.expanduser(export_dir), f"{solution_name}.xml")
        log.info("Using export_dir_override from automation.json: %r", path)
        return path

    # Auto-detect via PowerShell + wslpath (WSL2 only).
    # GetFolderPath('Desktop') resolves OneDrive-redirected Desktops correctly;
    # a simple USERPROFILE\Desktop approach misses this common Windows setup.
    try:
        r1 = subprocess.run(
            [
                "powershell.exe", "-NoProfile", "-Command",
                "[Environment]::GetFolderPath('Desktop')",
            ],
            capture_output=True, text=True, timeout=10,
        )
        if r1.returncode == 0:
            win_desktop = r1.stdout.strip()  # e.g. C:\Users\pchov\OneDrive\Desktop
            r2 = subprocess.run(
                ["wslpath", win_desktop],
                capture_output=True, text=True, timeout=5,
            )
            if r2.returncode == 0:
                wsl_desktop = r2.stdout.strip()  # e.g. /mnt/c/Users/pchov/OneDrive/Desktop
                path = os.path.join(wsl_desktop, f"{solution_name}.xml")
                log.info("Auto-detected WSL2 export path via PowerShell+wslpath: %r", path)
                return path
    except Exception as exc:
        log.warning("WSL2 export path auto-detection failed: %s", exc)

    return ""


def _incoming_export_path(path: str, solution_name: str) -> str:
    """The export path FileMaker sent, made usable here; "" when it is an unresolvable '?'.

    '?' means FileMaker's ConvertFromFileMakerPath failed. Under WSL the Desktop
    path is rebuilt (PowerShell + wslpath, or export_dir_override); anywhere else
    there is nothing to rebuild it from, so it is reported as unresolved.
    """
    path = os.path.expanduser(path)
    if path == "?":
        return _resolve_export_path_for_wsl(solution_name) if _is_wsl() else ""
    return _translate_incoming_path(path)


def _unresolved_export_path_error(field: str) -> str:
    if _is_wsl():
        return (
            f"{field} is '?' — FileMaker could not convert the Desktop path to POSIX "
            "and WSL2 auto-detection failed. "
            "Add 'export_dir_override' to agent/config/automation.json with the WSL2 path "
            "to your Windows Desktop (e.g. \"/mnt/c/Users/<you>/Desktop\"). "
            "See agent/docs/SANDBOXED_ENVIRONMENT.md for details."
        )
    return (
        f"{field} is '?' — FileMaker could not convert the export path. "
        "Send the full path of the export file or folder."
    )

# ---------------------------------------------------------------------------
# Webviewer process state (module-level, shared across request threads)
# ---------------------------------------------------------------------------

_webviewer_proc: "subprocess.Popen | None" = None
_webviewer_lock = threading.Lock()

# Pending paste job — set by /trigger before firing AppleScript,
# consumed by Agentic-fm Paste via GET /pending.
# Shape: {"target": str, "auto_save": bool}
_pending_job: dict = {}
_pending_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Plug-in capability detection (the AgenticFM commercial plug-in, optional)
# ---------------------------------------------------------------------------
#
# The companion is the single detection broker for the OSS agent. It stats the
# plug-in's macOS Application Support tree (the *installed?* precondition),
# reads the address + bearer token published in preferences.json, then probes
# the plug-in's token-free GET /api/health to resolve the *usable?* verdict
# (installed + reachable + licensed). The result is surfaced in /health.plugin
# so the agent makes exactly one detection call. The verdict is cached briefly
# so repeated /health hits do not re-probe the plug-in on every request.
#
# This is detection only — nothing here makes any OSS feature depend on the
# plug-in. When the plug-in is absent or not usable, the block reports it and
# the agent stays on the pure OSS path.

# NOTE: intentionally a fixed macOS platform path, NOT configurable via companion.json.
# It has exactly one correct value; only `~` varies, resolving to the macOS user running
# the companion — which must be the same user running FileMaker + the plug-in. A relocated
# or cross-user path is a deployment error to document, not a knob. See the plug-in
# detection note in agent/docs/COMPANION_SERVER.md.
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/AgenticFM")
_PLUGIN_CACHE_TTL = 60.0  # seconds; plug-in's own license heartbeat is far slower
_plugin_cache: dict = {}
_plugin_cache_ts: float = 0.0
_plugin_lock = threading.Lock()

# Behavior gates the plug-in enforces on mutating operations — surfaced so the
# OSS agent can respect them rather than stacking a redundant confirmation.
_PLUGIN_GATE_KEYS = (
    "agenticActions", "scriptExecution", "scriptModification",
    "schemaModification", "uiInteraction", "hostCapture",
)


def _plugin_http_get_json(url: str, token: str = "", timeout: int = 3) -> dict:
    """GET a JSON document from the plug-in's HTTP server. Raises on failure."""
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _plugin_read_preferences() -> dict:
    """Read the plug-in's preferences.json (address, token, gates, bindings)."""
    path = os.path.join(APP_SUPPORT_DIR, "preferences.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _plugin_enumerate_solutions() -> list:
    """Enumerate indexed solutions under solutions/<key>/manifest.json."""
    solutions = []
    sol_dir = os.path.join(APP_SUPPORT_DIR, "solutions")
    if not os.path.isdir(sol_dir):
        return solutions
    for key in sorted(os.listdir(sol_dir)):
        manifest_path = os.path.join(sol_dir, key, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue
        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
        except (OSError, ValueError):
            continue
        solutions.append({
            "key": key,
            "name": manifest.get("solutionName", ""),
            "catalog": os.path.join(sol_dir, key),
            "files": manifest.get("loaded", []),
            "parsed_at": manifest.get("parsedAt") or manifest.get("parsed_at", ""),
        })
    return solutions


def _probe_plugin() -> dict:
    """Resolve the full plug-in capability block. Never raises.

    Shape:
      {
        "installed": bool,            # App Support tree present
        "usable": bool,               # installed AND reachable AND licensed
        "server": {"reachable", "base", "token", "remote"},
        "license": {"status", "licensed", ...},
        "solutions": [ {key, name, catalog, files, parsed_at}, ... ],
        "repoPath"?, "catalogBaseUrl"?, "gates"?
      }
    """
    block = {"installed": False, "usable": False,
             "server": {}, "license": {}, "solutions": []}

    if not os.path.isdir(APP_SUPPORT_DIR):
        return block
    block["installed"] = True

    try:
        prefs = _plugin_read_preferences()
    except (OSError, ValueError):
        prefs = {}

    if prefs.get("repoPath"):
        block["repoPath"] = prefs["repoPath"]
    if prefs.get("catalogBaseUrl"):
        block["catalogBaseUrl"] = prefs["catalogBaseUrl"]
    gates = {k: prefs[k] for k in _PLUGIN_GATE_KEYS if k in prefs}
    if gates:
        block["gates"] = gates

    # Remote access is the supported posture: a stable port + token in
    # preferences.json. (Localhost-only random-port mode is not targeted.)
    port = prefs.get("remoteAccessPort", 8766)
    token = prefs.get("remoteAccessToken", "")
    remote_on = bool(prefs.get("allowRemoteAccess")) and bool(prefs.get("remoteAccessConfirmed"))
    base = f"http://127.0.0.1:{port}"
    block["server"] = {"reachable": False, "base": base, "token": token, "remote": remote_on}

    # Token-free GET /api/health carries the verdict: licensed + licenseStatus.
    try:
        health = _plugin_http_get_json(f"{base}/api/health", timeout=3)
        block["server"]["reachable"] = True
        licensed = bool(health.get("licensed"))
        lic = {"status": health.get("licenseStatus", ""), "licensed": licensed}
        # Pull richer detail (daysRemaining etc.) — exempt, best-effort.
        try:
            detail = _plugin_http_get_json(f"{base}/api/license", token=token, timeout=3)
            for k in ("status", "edition", "sub", "exp", "daysRemaining"):
                if k in detail:
                    lic[k] = detail[k]
        except Exception:
            pass
        block["license"] = lic
        block["usable"] = bool(licensed)
    except Exception:
        # Installed but server not reachable (or down) — not usable.
        block["license"] = {"status": "unknown", "licensed": False}
        block["usable"] = False

    # Broker the plug-in's self-describing endpoint suite. GET /api/discover is
    # token-free (exempt) and returns the *live*, license-gated catalog: the
    # full endpoint suite when usable, a shrunk list + purchaseUrl when locked.
    # The OSS agent reads this to choose endpoints for the task at hand rather
    # than assuming a fixed set — so the integration never hardcodes (or rots
    # against) the plug-in's API surface. Surfaced even when not usable so the
    # locked-state purchaseUrl is available for the lapsed-license nudge.
    if block["server"].get("reachable"):
        try:
            block["discover"] = _plugin_http_get_json(f"{base}/api/discover", timeout=3)
        except Exception:
            pass

    block["solutions"] = _plugin_enumerate_solutions()
    return block


def _check_plugin(force: bool = False) -> dict:
    """Return the plug-in capability block, cached ~60 s. Never raises."""
    global _plugin_cache, _plugin_cache_ts
    with _plugin_lock:
        now = time.monotonic()
        if not force and _plugin_cache and (now - _plugin_cache_ts) < _PLUGIN_CACHE_TTL:
            return _plugin_cache
        block = _probe_plugin()
        _plugin_cache = block
        _plugin_cache_ts = now
        return block

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("companion_server")
SUBPROCESS_HEARTBEAT_SECONDS = 5
# True while the live progress panel is drawn (_start_live_panel sets it, _stop_live_panel clears it).
# The panel shows the file in progress, so file_start lines (convert and export) then go to debug and
# the log keeps one line per file. Without the panel they stay at info: the only sign of life in a long file.
_live_panel_on = False


def _file_start_level() -> int:
    return logging.DEBUG if _live_panel_on else logging.INFO


# ---------------------------------------------------------------------------
# Log file (roadmap P3-18)
# ---------------------------------------------------------------------------
#
# The companion writes companion-server.log in the per-user state folder (companion_state),
# whatever started it. The file gets debug lines (stage progress, GET /convert polls); the
# console stays at info. The handler sits on the root logger, and companion_live keeps file
# handlers when it swaps the root handlers for the live panel's.

LOG_FILE_ENV = "COMPANION_LOG_FILE"
LAUNCHED_BY_ENV = "COMPANION_LAUNCHED_BY"
LAUNCHED_BY_CHOICES = ("terminal", "background", "logon task", "launchd", "ide")
_file_handler: "logging.Handler | None" = None


def _resolve_log_file(cli_value: "str | None", no_log_file: bool, comp: dict, environ=None) -> tuple:
    """(log file path or "" for none, where it came from).

    Order: --no-log-file, --log-file, $COMPANION_LOG_FILE, companion.json log_file, the state
    folder's companion-server.log. An empty value or "off" in any of them turns the file off.
    """
    environ = os.environ if environ is None else environ
    if no_log_file:
        return "", "--no-log-file"
    for value, source in ((cli_value, "--log-file"), (environ.get(LOG_FILE_ENV), f"${LOG_FILE_ENV}"),
                          (comp.get("log_file"), "companion.json log_file")):
        if value is None:
            continue
        text = str(value).strip()
        if not text or text.lower() == "off":
            return "", source
        return os.path.abspath(os.path.expanduser(text)), source
    return companion_state.default_log_file(), "default"


def _setup_file_logging(path: str) -> "logging.Handler | None":
    """Keep the console at info and, when path is set, add the rotating debug log file."""
    global _file_handler
    root = logging.getLogger()
    for handler in root.handlers:
        if not isinstance(handler, logging.FileHandler):
            handler.setLevel(max(handler.level, logging.INFO))
    if not path:
        return None
    try:
        handler = companion_state.make_file_handler(path)
    except OSError as exc:
        log.warning("Could not open the log file %s (%s); logging to the console only.", path, exc)
        return None
    root.addHandler(handler)
    log.setLevel(logging.DEBUG)
    _file_handler = handler
    return handler


def _teardown_file_logging() -> None:
    """Close the log file from _setup_file_logging (the end of main, and tests)."""
    global _file_handler
    if _file_handler is None:
        return
    logging.getLogger().removeHandler(_file_handler)
    # companion_live may still hold it in a swapped handler list; closing is enough there
    _file_handler.close()
    _file_handler = None
    log.setLevel(logging.NOTSET)


def _stream_pipe(pipe, level, prefix, output_buffer, state, on_line=None, transcript=None):
    """Copy a subprocess pipe to the logger in real time while buffering it.

    on_line, when set, sees each line first; a line it returns True for is handled
    (logged its own way) and is neither logged here nor buffered.
    transcript, when set, gets every line as it arrives, handled or not (the job's .log).
    """
    try:
        for line in iter(pipe.readline, ""):
            if not line:
                break
            with state["lock"]:
                state["last_output_at"] = time.monotonic()
            if transcript is not None:
                transcript.append(line)
            if on_line is not None and on_line(line):
                continue
            output_buffer.append(line)
            level("%s%s", prefix, line.rstrip("\n"))
    finally:
        pipe.close()


def _run_command_streaming(cmd, *, cwd, env, label, on_stdout_line=None, on_process=None, transcript=None):
    """Run a command, stream its output to the server log, and capture it.

    on_stdout_line: optional per-line hook for stdout (see _stream_pipe).
    on_process: optional, called with the Popen once started (so shutdown can stop it).
    transcript: optional list that gets every stdout and stderr line in arrival order.
    """
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if on_process is not None:
        on_process(process)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    state = {"last_output_at": time.monotonic(), "lock": threading.Lock()}

    stdout_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stdout, log.info, f"[{label} stdout] ", stdout_chunks, state, on_stdout_line, transcript),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_stream_pipe,
        args=(process.stderr, log.warning, f"[{label} stderr] ", stderr_chunks, state, None, transcript),
        daemon=True,
    )

    stdout_thread.start()
    stderr_thread.start()

    last_heartbeat_at = time.monotonic()
    while True:
        try:
            return_code = process.wait(timeout=1)
            break
        except subprocess.TimeoutExpired:
            now = time.monotonic()
            with state["lock"]:
                silence_for = now - state["last_output_at"]
            if (
                silence_for >= SUBPROCESS_HEARTBEAT_SECONDS
                and now - last_heartbeat_at >= SUBPROCESS_HEARTBEAT_SECONDS
            ):
                log.info(
                    "%s still running... (%ds since last output)",
                    label,
                    int(silence_for),
                )
                last_heartbeat_at = now

    stdout_thread.join()
    stderr_thread.join()

    return {
        "returncode": return_code,
        "stdout": "".join(stdout_chunks),
        "stderr": "".join(stderr_chunks),
    }


# ---------------------------------------------------------------------------
# fm-saxml conversion jobs (POST /convert, GET /convert/<job id>)
# ---------------------------------------------------------------------------
#
# A conversion runs fm-saxml in a subprocess on a worker thread, so the POST
# returns a job ID at once and FileMaker's Insert from URL never waits on it.
# fm-saxml runs under sys.executable: the companion is started with the DIS
# harness .venv's Python, which is where fm-saxml-converter is installed (see
# the launchers in agent/scripts/launchers/).

DOCS_ROOT_ENV = "FM_SAXML_DOCS_ROOT"
CONVERT_MAX_JOBS = 50           # finished jobs kept for polling; the oldest are dropped
CONVERT_MAX_WARNINGS = 200      # warnings listed per job; warning_count is always the full count
CONVERT_LOG_TAIL_CHARS = 4000
# Request fields that turn on an fm-saxml build switch of the same name. Only these: --serve
# would never finish (and hold the one conversion slot), --out would write outside the docs root.
CONVERT_SWITCHES = ("html", "webmcp")

# Counted per FileMaker file, from its entities.json: response key → entities.json collection.
CONVERT_COUNT_KEYS = {
    "tables": "tables",
    "fields": "fields",
    "table_occurrences": "tableOccurrences",
    "relationships": "relationships",
    "layouts": "layouts",
    "scripts": "scripts",
    "custom_functions": "customFunctions",
    "value_lists": "valueLists",
}

# fm-saxml's --progress json stage ids → labels for the log (same as its plain mode).
CONVERT_STAGE_LABELS = {
    "read": "Reading",
    "scrub": "Scrubbing",
    "analyze": "Analyzing",
    "write_raw": "Writing raw XML",
    "write_pages": "Writing pages",
    "write_viewer": "Writing HTML viewer",
    "check": "Checking redactions",
}
# Keys of a stage's unit count in the progress block (from stage_progress and stage_done).
CONVERT_UNIT_KEYS = ("done", "units", "unit", "stage_elapsed")

_convert_jobs: dict = {}        # job id → job dict; insertion-ordered
_convert_stage_starts: dict = {}    # job id → the "elapsed" of its current stage's stage_start
_convert_lock = threading.Lock()
_convert_slot = threading.Semaphore(1)   # one conversion at a time; the rest stay "queued"
# Whether the installed fm-saxml takes --progress json. Set to False the first time a build
# rejects it (an fm-saxml before P3-10), so later builds don't pay the retry; a restart resets it.
_convert_progress_supported = True
# For the job records on disk (P3-23): the command and fm-saxml's whole output per job, kept
# until _persist_job writes them; and the running fm-saxml process, so shutdown can stop it.
_convert_commands: dict = {}    # job id → the last command run
_convert_outputs: dict = {}     # job id → full stdout and stderr
_convert_processes: dict = {}   # job id → Popen while fm-saxml runs
JOB_RECORD_LAST = "last_job.json"
JOB_RECORD_IN_OUTPUT = "_companion_job.json"
# A --webmcp build's docs are served here once it succeeds (fm-saxml serve's default port;
# a free one is used when it's taken). Output folder → (server, url, output_path).
VIEWER_PORT = 8766
_viewer_servers: dict = {}
_viewer_lock = threading.Lock()

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_WARNING_TOTAL_RE = re.compile(r"_(\d+) warning\(s\) total_")
_WARNING_ROW_RE = re.compile(r"^\| `(?P<code>[^`]*)` \| (?P<entity>.*?) \| (?P<message>.*) \| (?P<detail>.*?) \|\s*$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# The agentic-fm root (this file is agent/scripts/companion_server.py).
AFM_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS_ROOT_SOURCES = {
    "request": "docs_root in the request",
    "env": f"${DOCS_ROOT_ENV}",
    "harness": "harness exports/",
}


def _harness_root(afm_root: "str | None" = None) -> str:
    """The DIS harness folder when agentic-fm is its tools/agentic-fm submodule, else "".

    Same test as the launchers (a submodule has a .git *file*), plus: the parent
    folder is named "tools" and the harness root has a .gitmodules.
    """
    root = os.path.abspath(afm_root or AFM_ROOT)
    harness = os.path.dirname(os.path.dirname(root))
    if (os.path.isfile(os.path.join(root, ".git"))
            and os.path.basename(os.path.dirname(root)).casefold() == "tools"
            and os.path.isfile(os.path.join(harness, ".gitmodules"))):
        return harness
    return ""


def _resolve_docs_root(requested: str = "", afm_root: "str | None" = None, create: bool = True) -> tuple:
    """(docs root, source key) for a conversion; ("", "") when none applies.

    Order: docs_root in the request, then $FM_SAXML_DOCS_ROOT, then the harness's
    exports/ folder (created if missing) when agentic-fm is the harness submodule.
    """
    if requested:
        return _translate_incoming_path(os.path.expanduser(str(requested))), "request"
    env = os.environ.get(DOCS_ROOT_ENV, "")
    if env:
        return os.path.expanduser(env), "env"
    harness = _harness_root(afm_root)
    if harness:
        exports = os.path.join(harness, "exports")
        if create:
            try:
                os.makedirs(exports, exist_ok=True)
            except OSError as exc:
                log.warning("Could not create the harness exports folder %s: %s", exports, exc)
        return exports, "harness"
    return "", ""


def _docs_root_startup_message() -> str:
    """The default docs root for /convert and where it comes from, for the startup log."""
    docs_root, source = _resolve_docs_root()
    if docs_root:
        return f"Docs root: {docs_root} ({DOCS_ROOT_SOURCES[source]})"
    return (f"Docs root: none set — /convert requests must send docs_root (or set {DOCS_ROOT_ENV}, "
            "or run agentic-fm as the DIS harness submodule).")


def _no_docs_root_error() -> str:
    return (
        "No docs root: send docs_root in the request, set "
        f"{DOCS_ROOT_ENV} where the companion runs, or run agentic-fm as the DIS harness's "
        "tools/agentic-fm submodule (the docs then go to the harness's exports/ folder)."
    )


def _convert_command(export_path: str, docs_root: str, solution_name: str, *,
                     html: bool = False, webmcp: bool = False, progress: bool = True) -> list:
    """The fm-saxml command for one conversion (tests replace this).

    progress=False leaves out --progress json, for an fm-saxml that doesn't have it.
    """
    cmd = [
        sys.executable, "-m", "fm_saxml", "build", export_path,
        "--docs-root", docs_root, "--solution", solution_name,
        "--force", "--yes",
    ]
    if progress:
        cmd += ["--progress", "json"]
    if html:
        cmd.append("--html")
    if webmcp:
        cmd.append("--webmcp")
    return cmd


def _convert_env() -> dict:
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1", "TERM": "dumb",
                "COLUMNS": "120"})
    return env


def _find_output_folder(docs_root: str, solution_name: str) -> str:
    """The folder fm-saxml wrote for solution_name: <docs root>/<slug>, found by docs.json."""
    direct = os.path.join(docs_root, solution_name)
    if os.path.isfile(os.path.join(direct, "docs.json")):
        return direct
    try:
        names = sorted(os.listdir(docs_root))
    except OSError:
        return ""
    for name in names:
        catalog = os.path.join(docs_root, name, "docs.json")
        try:
            with open(catalog, "r", encoding="utf-8") as f:
                solution = json.load(f).get("solution", "")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(solution, str) and solution.casefold() == solution_name.casefold():
            return os.path.join(docs_root, name)
    return ""


def _read_warnings(report_path: str) -> tuple:
    """(count, [{code, entity, message}]) from a file's Reports/warnings.md.

    fm-saxml writes warnings only to this report (not to JSON); messages there are
    cut at 80 characters.
    """
    try:
        with open(report_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return 0, []
    rows = []
    for line in text.splitlines():
        m = _WARNING_ROW_RE.match(line)
        if m:
            entity = m.group("entity").strip()
            rows.append({
                "code": m.group("code"),
                "entity": "" if entity == "_" else entity,
                "message": m.group("message").strip(),
            })
    m = _WARNING_TOTAL_RE.search(text)
    return (int(m.group(1)) if m else len(rows)), rows


def _summarize_output(output_path: str) -> dict:
    """Counts and warnings for a finished conversion, read from fm-saxml's output files."""
    with open(os.path.join(output_path, "docs.json"), "r", encoding="utf-8") as f:
        catalog = json.load(f)
    totals = {key: 0 for key in CONVERT_COUNT_KEYS}
    files, warnings, warning_count = [], [], 0
    for entry in catalog.get("files", []):
        name = entry.get("name", "")
        folder = os.path.join(output_path, entry.get("folder") or "")
        counts = {key: 0 for key in CONVERT_COUNT_KEYS}
        try:
            with open(os.path.join(folder, "entities.json"), "r", encoding="utf-8") as f:
                entities = json.load(f)
            for key, collection in CONVERT_COUNT_KEYS.items():
                counts[key] = len(entities.get(collection) or {})
        except (OSError, ValueError) as exc:
            log.warning("convert: could not count %s's entities: %s", name, exc)
        file_warning_count, file_warnings = _read_warnings(os.path.join(folder, "Reports", "warnings.md"))
        for key in totals:
            totals[key] += counts[key]
        warning_count += file_warning_count
        warnings.extend({"file": name, **w} for w in file_warnings)
        files.append({"name": name, "counts": counts, "warning_count": file_warning_count})
    totals["files"] = len(files)
    viewer = os.path.join(output_path, "index.html")
    return {
        "kind": catalog.get("kind", ""),
        "viewer_path": viewer if os.path.isfile(viewer) else "",
        "counts": totals,
        "files": files,
        "warning_count": warning_count,
        "warnings": warnings[:CONVERT_MAX_WARNINGS],
    }


def _log_tail(text: str) -> str:
    text = _ANSI_RE.sub("", text or "")
    return text[-CONVERT_LOG_TAIL_CHARS:]


def _update_job(job_id: str, **fields) -> None:
    with _convert_lock:
        job = _convert_jobs.get(job_id)
        if job is not None:
            job.update(fields)


def _parse_progress_line(line: str):
    """The event dict when line is one of fm-saxml's --progress json lines, else None."""
    text = line.strip()
    if not text.startswith("{"):
        return None
    try:
        event = json.loads(text)
    except ValueError:
        return None
    return event if isinstance(event, dict) and "event" in event else None


def _seconds(value) -> str:
    return f"{value:.1f}s" if isinstance(value, (int, float)) and not isinstance(value, bool) else "?s"


def _progress_message(event: dict) -> str:
    """One readable log line for a progress event, e.g. "Analyzing 3/12 Invoices (4.2s)"."""
    kind = event.get("event")
    stage = event.get("stage") or ""
    label = CONVERT_STAGE_LABELS.get(stage, stage.replace("_", " ").capitalize())
    counter = f"{event.get('index')}/{event.get('total')}"
    if kind == "stage_start":
        return f"{label}: {event.get('total')} file(s)"
    if kind == "file_start":
        return f"{label} {counter} {event.get('file')}"
    if kind == "file_done":
        return f"{label} {counter} {event.get('file')} ({_seconds(event.get('seconds'))})"
    if kind == "stage_progress":
        return f"{label}: {event.get('done')}/{event.get('units')} {event.get('unit') or ''}".rstrip()
    if kind == "stage_done" and event.get("done") is not None:
        count = f"{event.get('done')} {event.get('unit') or ''}".rstrip()
        return f"{label} done: {count} ({_seconds(event.get('seconds'))})"
    if kind == "stage_done":
        return f"{label} done ({_seconds(event.get('seconds'))})"
    if kind == "done":
        return f"fm-saxml done ({_seconds(event.get('elapsed'))})"
    return " ".join(str(part) for part in (kind, label, counter, event.get("file") or "") if part).strip()


def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _record_progress(job_id: str, event: dict) -> None:
    """Fold one progress event into the job's progress block (see GET /convert in COMPANION_SERVER.md).

    A stage with units (stage_progress, and stage_done with done) adds done, units, unit and
    stage_elapsed; the next stage_start, or the build's done, removes them.
    """
    kind = event.get("event")
    with _convert_lock:
        job = _convert_jobs.get(job_id)
        if job is None:
            return
        progress = job.setdefault("progress", {})
        progress.update({key: event.get(key) for key in ("stage", "file", "index", "total", "elapsed")})
        files = progress.setdefault("files", [])
        if kind == "file_done":
            files.append({"stage": event.get("stage"), "file": event.get("file"),
                          "seconds": event.get("seconds")})
        elif kind in ("stage_start", "done"):
            for key in CONVERT_UNIT_KEYS:
                progress.pop(key, None)
            if kind == "stage_start":
                _convert_stage_starts[job_id] = event.get("elapsed")
            else:
                _convert_stage_starts.pop(job_id, None)
        elif kind == "stage_progress" or (kind == "stage_done" and _is_number(event.get("done"))):
            done = event.get("done")
            # stage_done may leave out units: keep the last stage_progress's, else the final count
            units = event.get("units", progress.get("units", done))
            progress.update(done=done, units=units, unit=event.get("unit") or progress.get("unit"))
            started, elapsed = _convert_stage_starts.get(job_id), event.get("elapsed")
            if _is_number(started) and _is_number(elapsed):
                progress["stage_elapsed"] = round(max(0.0, elapsed - started), 1)
            elif kind == "stage_done" and _is_number(event.get("seconds")):
                progress["stage_elapsed"] = event["seconds"]


def _progress_rejected(stderr: str) -> bool:
    """True when fm-saxml's own option parser refused --progress (an fm-saxml before P3-10)."""
    text = _ANSI_RE.sub("", stderr or "")
    return "No such option" in text and "--progress" in text


def _serve_viewer(output_path: str) -> str:
    """Serve a --webmcp build's docs at http://127.0.0.1 with fm-saxml's serve; the URL, or "". Never raises.

    Browsers give the viewer WebMCP only on a served page, not one opened from disk. One
    server per output folder, kept until the companion stops: a rebuild into the same folder
    reuses it (fm-saxml's server sends no-store, so a reload shows the new docs).
    """
    key = os.path.normcase(os.path.abspath(output_path))
    with _viewer_lock:
        if key in _viewer_servers:
            return _viewer_servers[key][1]
        try:
            from pathlib import Path
            from fm_saxml.serve import has_webmcp, make_server, url_for
        except ImportError as exc:
            log.warning("Could not serve the viewer (this fm-saxml has no serve: %s). "
                        "Run `fm-saxml serve %s` yourself.", exc, output_path)
            return ""
        try:
            if not has_webmcp(Path(output_path)):
                log.warning("Not serving %s: it has no viewer with WebMCP tools.", output_path)
                return ""
            # 0 (any free port) when taken: on Windows, an fm-saxml without the SO_EXCLUSIVEADDRUSE
            # fix binds a port another server listens on, so its own fallback never runs
            server = make_server(Path(output_path), 0 if _port_listening(VIEWER_PORT) else VIEWER_PORT)
        except OSError as exc:
            log.warning("Could not serve the viewer from %s: %s", output_path, exc)
            return ""
        port = server.server_address[1]
        if port != VIEWER_PORT:
            log.info("Port %d is in use; serving the viewer on %d.", VIEWER_PORT, port)
        url = url_for(server)
        threading.Thread(target=server.serve_forever, daemon=True, name=f"viewer-{port}").start()
        _viewer_servers[key] = (server, url, output_path)
        return url


def _port_listening(port: int) -> bool:
    """Whether something accepts connections on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _served_viewers() -> list:
    """[{"output_path", "url"}] of the viewers being served, for /health and status."""
    with _viewer_lock:
        return [{"output_path": path, "url": url} for _, url, path in _viewer_servers.values()]


def _stop_viewer_servers() -> None:
    with _viewer_lock:
        servers = [server for server, _, _ in _viewer_servers.values()]
        _viewer_servers.clear()
    for server in servers:
        try:
            server.shutdown()
            server.server_close()
        except Exception as exc:
            log.warning("Could not stop a viewer server: %s", exc)


def _viewer_banner(solution_name: str, url: str) -> str:
    """The served viewer's link, set off from the log lines around it."""
    rule = "=" * 72
    return f"\n{rule}\n  WebMCP viewer for {solution_name}:\n\n    {url}\n{rule}"


def _run_convert_job(job_id: str) -> None:
    """Worker thread: run one conversion and record its outcome. Never raises."""
    global _convert_progress_supported
    with _convert_lock:
        job = dict(_convert_jobs[job_id])

    def on_line(line: str) -> bool:
        event = _parse_progress_line(line)
        if event is None:
            return False
        _record_progress(job_id, event)
        kind = event.get("event")
        # stage_progress only updates the job; file_start is the panel's job while it is drawn
        level = (logging.DEBUG if kind == "stage_progress"
                 else _file_start_level() if kind == "file_start" else logging.INFO)
        log.log(level, "convert %s: %s", job_id, _progress_message(event))
        return True

    def on_process(process) -> None:
        with _convert_lock:
            _convert_processes[job_id] = process

    transcript: list = []

    def run(progress: bool) -> dict:
        extra = {} if progress else {"progress": False}
        cmd = _convert_command(job["export_path"], job["docs_root"], job["solution_name"],
                               html=job["html"], webmcp=job["webmcp"], **extra)
        log.info("convert %s: %s", job_id, " ".join(cmd))
        with _convert_lock:
            _convert_commands[job_id] = list(cmd)
        start = len(transcript)
        result = _run_command_streaming(cmd, cwd=None, env=_convert_env(), label=f"fm-saxml {job_id}",
                                        on_stdout_line=on_line, on_process=on_process, transcript=transcript)
        if len(transcript) == start:     # a runner that doesn't fill the transcript (tests)
            transcript.append(result["stdout"] + result["stderr"])
        with _convert_lock:
            _convert_outputs[job_id] = "".join(transcript)
        return result

    try:
        with _convert_slot:
            if _job_done(job_id):    # ended by a shutdown while it was queued
                return
            _update_job(job_id, state="running", started_at=_utc_now())
            with_progress = _convert_progress_supported
            result = run(with_progress)
            if with_progress and result["returncode"] == 2 and _progress_rejected(result["stderr"]):
                _convert_progress_supported = False
                log.info("convert %s: this fm-saxml has no --progress; running it again without", job_id)
                _update_job(job_id, progress={})
                result = run(False)
        if _job_done(job_id):        # ended by a shutdown (_end_active_jobs): keep that outcome
            return
        output = result["stdout"] + result["stderr"]
        if result["returncode"] != 0:
            _update_job(job_id, state="failed", done=True, finished_at=_utc_now(),
                        exit_code=result["returncode"],
                        error=f"fm-saxml exited with code {result['returncode']}",
                        log_tail=_log_tail(output))
            log.warning("convert %s failed: exit code %d", job_id, result["returncode"])
            return
        output_path = _find_output_folder(job["docs_root"], job["solution_name"])
        if not output_path:
            _update_job(job_id, state="failed", done=True, finished_at=_utc_now(), exit_code=0,
                        error=f"fm-saxml finished but no docs.json for {job['solution_name']!r} "
                              f"under {job['docs_root']}",
                        log_tail=_log_tail(output))
            log.warning("convert %s: no output folder found", job_id)
            return
        summary = _summarize_output(output_path)
        # served before the job is done, so a poller that sees "succeeded" also sees the URL
        viewer_url = _serve_viewer(output_path) if job["webmcp"] and summary["viewer_path"] else ""
        _update_job(job_id, state="succeeded", done=True, finished_at=_utc_now(), exit_code=0,
                    output_path=output_path, viewer_url=viewer_url, **summary)
        log.info("convert %s succeeded: %s (%d file(s), %d warning(s))",
                 job_id, output_path, summary["counts"]["files"], summary["warning_count"])
        if viewer_url:
            log.info(_viewer_banner(job["solution_name"], viewer_url))
    except Exception as exc:    # a crash in the runner must not take the server down
        log.exception("convert %s crashed: %s", job_id, exc)
        with _convert_lock:     # the traceback goes into the job's .log
            _convert_outputs[job_id] = _convert_outputs.get(job_id, "") + traceback.format_exc()
        if not _job_done(job_id):
            _update_job(job_id, state="failed", done=True, finished_at=_utc_now(),
                        error=f"Conversion crashed: {exc}")
    finally:
        with _convert_lock:
            _convert_stage_starts.pop(job_id, None)
            _convert_processes.pop(job_id, None)
        _persist_job(job_id)


def _job_done(job_id: str) -> bool:
    with _convert_lock:
        job = _convert_jobs.get(job_id)
        return job is None or bool(job.get("done"))


def _write_text_atomic(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def _prune_job_records(folder: str, keep: "int | None" = None) -> None:
    """Keep the newest `keep` (default CONVERT_MAX_JOBS) job records (<job id>.json and .log) in folder."""
    keep = CONVERT_MAX_JOBS if keep is None else keep
    records = []
    for name in os.listdir(folder):
        if name.endswith(".json") and name != JOB_RECORD_LAST:
            path = os.path.join(folder, name)
            try:
                records.append((os.path.getmtime(path), name))
            except OSError:
                continue
    records.sort(reverse=True)
    for _, name in records[keep:]:
        stem = name[: -len(".json")]
        for old in (name, stem + ".log"):
            try:
                os.remove(os.path.join(folder, old))
            except FileNotFoundError:
                pass


def _persist_job(job_id: str) -> None:
    """Write a finished job's record to the state folder's jobs/ (roadmap P3-23). Never raises.

    jobs/<job id>.json is the job as GET /convert/<job id> returns it plus "command" and
    "log_file"; jobs/<job id>.log is fm-saxml's whole output; jobs/last_job.json is the
    latest record. Also writes _companion_job.json into the output folder when there is one.
    A failed write is logged; the job itself is unaffected.
    """
    with _convert_lock:
        job = _convert_jobs.get(job_id)
        job = json.loads(json.dumps(job)) if job is not None else None
        command = _convert_commands.pop(job_id, None)
        output = _convert_outputs.pop(job_id, "")
    if job is None:
        return
    try:
        folder = companion_state.jobs_dir()
        log_path = os.path.join(folder, f"{job_id}.log")
        record = {**job, "command": command, "log_file": log_path}
        text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        _write_text_atomic(log_path, output)
        _write_text_atomic(os.path.join(folder, f"{job_id}.json"), text)
        _write_text_atomic(os.path.join(folder, JOB_RECORD_LAST), text)
        _prune_job_records(folder)
        log.debug("convert %s: record written to %s", job_id, folder)
    except Exception as exc:
        log.warning("convert %s: could not write its record to the jobs folder: %s", job_id, exc)
        return
    output_path = job.get("output_path") or ""
    if output_path and os.path.isdir(output_path):
        try:
            _write_text_atomic(os.path.join(output_path, JOB_RECORD_IN_OUTPUT), text)
        except Exception as exc:
            log.warning("convert %s: could not write %s in %s: %s",
                        job_id, JOB_RECORD_IN_OUTPUT, output_path, exc)


def _start_convert_job(solution_name: str, export_path: str, docs_root: str, *,
                       html: bool = False, webmcp: bool = False) -> tuple:
    """Queue a conversion. Returns (job, None), or (None, running job) for a solution already converting."""
    with _convert_lock:
        for other in _convert_jobs.values():
            if other["solution_name"].casefold() == solution_name.casefold() and not other["done"]:
                return None, dict(other)
        job_id = uuid.uuid4().hex[:12]
        job = {
            "job_id": job_id,
            "state": "queued",
            "done": False,
            "solution_name": solution_name,
            "export_path": export_path,
            "docs_root": docs_root,
            "html": html,
            "webmcp": webmcp,
            "output_path": "",
            "created_at": _utc_now(),
            "started_at": "",
            "finished_at": "",
            "exit_code": None,
            "error": "",
            "progress": {},     # filled from fm-saxml's --progress json lines; stays {} without them
        }
        _convert_jobs[job_id] = job
        finished = [jid for jid, j in _convert_jobs.items() if j["done"]]
        for jid in finished[: max(0, len(finished) - CONVERT_MAX_JOBS)]:
            del _convert_jobs[jid]
        snapshot = dict(job)
    threading.Thread(target=_run_convert_job, args=(job_id,), daemon=True,
                     name=f"convert-{job_id}").start()
    return snapshot, None


def _get_convert_job(job_id: str):
    with _convert_lock:
        job = _convert_jobs.get(job_id)
        return json.loads(json.dumps(job)) if job is not None else None


# ---------------------------------------------------------------------------
# Export progress (POST /progress)
# ---------------------------------------------------------------------------
#
# FileMaker's export script reports each Save a Copy as XML before (file_start)
# and after (file_done) it runs, and once (done) after the last file, so the
# companion can show the export step, which happens before /convert is called.
# Only the latest run of each solution is kept, keyed by its casefolded name.

PROGRESS_EVENTS = ("file_start", "file_done", "done")

_export_progress: dict = {}
_export_file_starts: dict = {}   # casefolded solution → {(index, casefolded file): monotonic of its file_start}
_export_lock = threading.Lock()


def _progress_int(value):
    """value as an int >= 0, or None. Insert from URL may send numbers as text ("3")."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value >= 0 else None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        return int(value.strip())
    return None


def _validate_progress(payload) -> tuple:
    """Check a POST /progress body. Returns (event, None), or (None, error message)."""
    if not isinstance(payload, dict):
        return None, "Request body must be a JSON object"
    errors = []
    for name in ("solution_name", "stage"):
        if not isinstance(payload.get(name), str) or not payload[name].strip():
            errors.append(f"{name} must be a non-empty string")
    event = payload.get("event")
    if event not in PROGRESS_EVENTS:
        errors.append(f"event must be one of {', '.join(PROGRESS_EVENTS)}")
    file = payload.get("file")
    if not isinstance(file, str) or (event != "done" and not file.strip()):
        errors.append("file must be a string, not empty except for done")
    numbers = {}
    for name in ("index", "total"):
        numbers[name] = _progress_int(payload.get(name))
        if numbers[name] is None:
            errors.append(f"{name} must be a whole number >= 0")
    if (not errors and event != "done"
            and not 1 <= numbers["index"] <= numbers["total"]):
        errors.append("index must be between 1 and total")
    if errors:
        return None, "; ".join(errors)
    return {"solution_name": payload["solution_name"].strip(), "stage": payload["stage"].strip(),
            "event": event, "file": file.strip(), **numbers}, None


def _record_export_progress(event: dict, now: "float | None" = None) -> dict:
    """Record one validated export event, log it, and return a copy of that solution's state.

    A new run starts with a file_start of index 1, or with any file event after a done.
    file_done takes its time from the file_start of the same index and file.
    """
    now = time.monotonic() if now is None else now
    key = event["solution_name"].casefold()
    kind = event["event"]
    with _export_lock:
        state = _export_progress.get(key)
        if (state is None or (state["done"] and kind != "done")
                or (kind == "file_start" and event["index"] == 1)):
            state = {"solution_name": event["solution_name"], "stage": event["stage"], "event": kind,
                     "file": "", "index": 0, "total": 0, "started_at": now, "updated_at": now,
                     "files": [], "done": False}
            _export_progress[key] = state
            _export_file_starts[key] = {}
        starts = _export_file_starts.setdefault(key, {})
        file_key = (event["index"], event["file"].casefold())
        seconds = None
        if kind == "file_start":
            starts[file_key] = now
        elif kind == "file_done":
            started = starts.pop(file_key, None)
            seconds = round(now - started, 1) if started is not None else None
            state["files"].append({"file": event["file"], "seconds": seconds})
        state.update(solution_name=event["solution_name"], stage=event["stage"], event=kind, updated_at=now)
        if kind == "done":
            state["done"] = True
            if event["total"]:      # a done may carry 0/0: keep the last file's N of M
                state.update(index=event["index"], total=event["total"])
        else:
            state.update(file=event["file"], index=event["index"], total=event["total"])
        snapshot = json.loads(json.dumps(state))
    name = event["solution_name"]
    if kind == "file_start":
        log.log(_file_start_level(), "export %s: Exporting %d/%d %s", name, event["index"], event["total"],
                event["file"])
    elif kind == "file_done" and seconds is not None:
        log.info("export %s: Exported %d/%d %s (%.1fs)", name, event["index"], event["total"],
                 event["file"], seconds)
    elif kind == "file_done":
        log.info("export %s: Exported %d/%d %s (no matching file_start, time unknown)", name,
                 event["index"], event["total"], event["file"])
    else:
        log.info("export %s: Export done: %d file(s) in %.1fs", name, len(snapshot["files"]),
                 now - snapshot["started_at"])
    return snapshot


def _get_export_progress(solution_name: "str | None" = None):
    """A copy of one solution's latest export state (None if it has none), or with no name,
    of every solution's, keyed by casefolded name."""
    with _export_lock:
        if solution_name is None:
            return json.loads(json.dumps(_export_progress))
        state = _export_progress.get(solution_name.casefold())
        return json.loads(json.dumps(state)) if state is not None else None


# ---------------------------------------------------------------------------
# Idle activity tracking
# ---------------------------------------------------------------------------

_last_activity = time.monotonic()
_activity_lock = threading.Lock()


def _touch_activity() -> None:
    global _last_activity
    with _activity_lock:
        _last_activity = time.monotonic()


def _seconds_since_activity() -> float:
    with _activity_lock:
        return time.monotonic() - _last_activity


def _convert_job_active() -> bool:
    with _convert_lock:
        return any(not job["done"] for job in _convert_jobs.values())


def _active_convert_job() -> "dict | None":
    """The running conversion, else the oldest queued one, as GET /convert returns it; None when idle."""
    with _convert_lock:
        active = [job for job in _convert_jobs.values() if not job["done"]]
        running = [job for job in active if job["state"] == "running"]
        job = (running or active or [None])[0]
        return json.loads(json.dumps(job)) if job is not None else None


def _idle_shutdown_due(idle_timeout: int) -> bool:
    """True once idle_timeout seconds have passed with no requests and no conversion queued or running.

    A conversion counts as activity: nobody may be polling it (FileMaker's loop
    can be cancelled), and shutting down would kill fm-saxml partway through.
    """
    return _seconds_since_activity() >= idle_timeout and not _convert_job_active()


def _idle_watchdog(server, idle_timeout: int) -> None:
    """Shut the server down after idle_timeout seconds with no requests."""
    check_interval = min(30, idle_timeout)
    while True:
        time.sleep(check_interval)
        idle = _seconds_since_activity()
        if _idle_shutdown_due(idle_timeout):
            log.info("Idle for %.0fs (timeout %ds) — shutting down.", idle, idle_timeout)
            server.shutdown()  # unblocks serve_forever() in the main thread
            return


def _clipboard_script() -> str:
    """clipboard.py, the one clipboard entry point; it picks the macOS or Windows backend."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "clipboard.py")


TRIGGER_BACKENDS = ("url", "applescript")


def _default_trigger_backend() -> str:
    """AppleScript on macOS (it can address FileMaker by app name), an fmp:// URL elsewhere."""
    return "applescript" if sys.platform == "darwin" else "url"


def fmp_script_url(target_file: str, script: str, parameter: str = "") -> str:
    """The fmp:// URL that runs `script` in the open file `target_file`.

    `$` addresses a file already open in FileMaker Pro. File, script and
    parameter are percent-encoded as UTF-8 with nothing left safe, so spaces,
    `&`, `=`, `?`, `#`, `/` and non-ASCII text survive. An empty parameter is left out.
    """
    def enc(s: str) -> str:
        return urllib.parse.quote(s, safe="")

    url = f"fmp://$/{enc(target_file)}?script={enc(script)}"
    if parameter:
        url += f"&param={enc(parameter)}"
    return url


def _open_fmp_url(url: str) -> None:
    """Hand an fmp:// URL to the OS, which passes it to FileMaker Pro. Raises OSError on failure."""
    if sys.platform == "win32":
        os.startfile(url)
    elif sys.platform == "darwin":
        result = subprocess.run(["open", url], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise OSError(result.stderr.strip() or f"open exited with {result.returncode}")
    else:
        raise OSError("fmp:// URLs need FileMaker Pro, on Windows or macOS")


# ---------------------------------------------------------------------------
# Threading HTTP server (handles concurrent requests)
# ---------------------------------------------------------------------------

class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """HTTPServer with thread-per-request concurrency.

    On Windows, SO_REUSEADDR lets a second process bind a port that is already
    listening (elsewhere it only allows rebinding during TIME_WAIT). Two companions
    would then share 8765 and FileMaker's requests would go to either one, so on
    Windows the port is taken exclusively: a second companion fails to start.
    """
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def server_bind(self):
        if sys.platform == "win32":
            # Also keeps out a later process that does set SO_REUSEADDR (an older companion)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _bind_server(host: str, port: int) -> "ThreadingHTTPServer | None":
    """The server bound to host:port, or None when the port is in use."""
    try:
        return ThreadingHTTPServer((host, port), CompanionHandler)
    except OSError as exc:
        # EADDRINUSE; on Windows WSAEADDRINUSE (10048), or WSAEACCES (10013) against an exclusive bind
        if exc.errno == errno.EADDRINUSE or getattr(exc, "winerror", None) in (10048, 10013):
            return None
        raise


def _client_host(bind_host: str) -> str:
    """Where a client on this machine reaches a server bound to bind_host."""
    if bind_host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if bind_host == "::":
        return "::1"
    return bind_host


# ---------------------------------------------------------------------------
# Who this companion is (GET /health, the state file; roadmap P3-19) and stopping it (P3-20)
# ---------------------------------------------------------------------------

_started_monotonic = time.monotonic()
# Filled by _init_server_info in main(); /health falls back to these for a server started otherwise (tests).
_server_info: dict = {
    "pid": os.getpid(),
    "started_at": _utc_now(),
    "host": DEFAULT_BIND_HOST,
    "port": None,
    "afm_root": AFM_ROOT,
    "python": sys.executable,
    "log_file": None,
    "launched_by": "terminal",
}
_state_token = ""           # the state file's token; POST /shutdown must send it. "" refuses every request.
_stopping = False           # set by POST /shutdown: new conversions are refused
_shutdown_reason = ""       # what ended the active conversions, for their records


def _init_server_info(host: str, port: int, log_file: str, launched_by: str) -> dict:
    global _started_monotonic
    _started_monotonic = time.monotonic()
    _server_info.update(pid=os.getpid(), started_at=_utc_now(), host=host, port=port,
                        afm_root=AFM_ROOT, python=sys.executable, log_file=log_file or None,
                        launched_by=launched_by)
    return dict(_server_info)


def _health(server_address: "tuple | None" = None) -> dict:
    """GET /health: status, version, plug-in, and who this companion is (never the token)."""
    info = dict(_server_info)
    if info.get("port") is None and server_address:
        info["host"], info["port"] = server_address[0], server_address[1]
    return {
        "status": "ok", "version": VERSION, "plugin": _check_plugin(),
        **info,
        "uptime_seconds": round(time.monotonic() - _started_monotonic, 1),
        "conversion": _active_convert_job(),
        "viewers": _served_viewers(),
    }


def _write_state_file() -> "str | None":
    """Write companion.state.json with a new token. Returns its path, or None when it could not."""
    global _state_token
    _state_token = companion_state.new_token()
    try:
        return companion_state.write_state({**_server_info, "token": _state_token})
    except OSError as exc:
        log.warning("Could not write the state file (%s): `companion_server.py stop` and `status` "
                    "won't find this companion.", exc)
        return None


def _launched_by(cli_value: "str | None", environ=None) -> str:
    environ = os.environ if environ is None else environ
    value = (cli_value or environ.get(LAUNCHED_BY_ENV) or "terminal").strip()
    if value not in LAUNCHED_BY_CHOICES:
        log.warning("Unknown launched_by %r (expected one of %s); kept as given.",
                    value, ", ".join(LAUNCHED_BY_CHOICES))
    return value


def _shutdown_when_idle(server, poll: float = 0.5) -> None:
    """POST /shutdown with wait: stop the server once no conversion is queued or running."""
    while _convert_job_active():
        time.sleep(poll)
    log.info("Conversion finished; stopping as asked by POST /shutdown.")
    server.shutdown()


STOP_SIGNALS = ("SIGTERM", "SIGBREAK")     # SIGBREAK exists on Windows only (Ctrl-Break)


def _stop_signal_handler(server):
    """A signal handler that stops the server like Ctrl-C: serve_forever returns into main()'s finally.

    shutdown() runs on its own thread: called on the thread running serve_forever, it would wait forever.
    """
    def handler(signum, frame):
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        log.info("Got %s; shutting down.", name)
        threading.Thread(target=server.shutdown, daemon=True, name="shutdown").start()
    return handler


def _install_stop_signals(server) -> dict:
    """Stop cleanly (state file removed, exit 0) on SIGTERM, and on SIGBREAK on Windows.

    `stop` falls back to SIGTERM, and launchd's KeepAlive restarts a companion killed by a signal.
    Returns the previous handlers for _restore_signals. Only the main thread can set handlers.
    """
    if threading.current_thread() is not threading.main_thread():
        return {}
    handler, previous = _stop_signal_handler(server), {}
    for name in STOP_SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, handler)
        except (OSError, ValueError) as exc:
            log.debug("Could not handle %s: %s", name, exc)
    return previous


def _restore_signals(previous: dict) -> None:
    for signum, old in previous.items():
        try:
            signal.signal(signum, old if old is not None else signal.SIG_DFL)
        except (OSError, ValueError, TypeError):
            pass


def _end_active_jobs(reason: str) -> None:
    """At shutdown: stop fm-saxml, mark unfinished conversions failed and write their records."""
    with _convert_lock:
        active = [jid for jid, job in _convert_jobs.items() if not job["done"]]
        processes = [_convert_processes.get(jid) for jid in active]
        for jid in active:
            _convert_jobs[jid].update(state="failed", done=True, finished_at=_utc_now(), error=reason)
    for jid, process in zip(active, processes):
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError as exc:
                log.warning("convert %s: could not stop fm-saxml (pid %d): %s", jid, process.pid, exc)
        log.warning("convert %s: %s", jid, reason)
        _persist_job(jid)


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class CompanionHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        """Route access log through the standard logger.

        GET /convert/<job id> goes at debug: FileMaker polls it every second or so, and at
        info those lines would bury the conversion's own progress lines. So does a successful
        POST /progress, which already logs its own readable line per event. So does GET /health,
        which monitors, `status` and `attach` (every few seconds) poll.
        """
        command = getattr(self, "command", None)
        path = str(getattr(self, "path", ""))
        # log_request passes (request line, status code, size); errors come through other formats.
        succeeded = len(args) == 3 and str(args[1]).startswith("2")
        quiet = ((command == "GET" and (path.startswith("/convert/") or path == "/health"))
                 or (command == "POST" and path == "/progress" and succeeded))
        (log.debug if quiet else log.info)("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self):
        # /health is a liveness probe — used by monitors and the status-bar
        # marker, which poll it continuously. It must NOT count as activity, or
        # that polling would keep the idle watchdog from ever firing and defeat
        # the auto-shutdown. Every other endpoint is real work and does count.
        if self.path != "/health":
            _touch_activity()
        if self.path == "/health":
            self._handle_health()
        elif self.path == "/pending":
            self._handle_pending_get()
        elif self.path == "/webviewer/status":
            self._handle_webviewer_status()
        elif self.path == "/clipboard":
            self._handle_clipboard_read()
        elif self.path.startswith("/convert/"):
            self._handle_convert_get(self.path[len("/convert/"):].split("?", 1)[0])
        elif self.path.startswith("/preview/"):
            layout_name = self.path[len("/preview/"):]
            self._handle_preview_get(layout_name)
        elif self.path.startswith("/plugin/"):
            self._handle_plugin_proxy("GET", self.path[len("/plugin/"):])
        else:
            self._send_json({"error": "Not found"}, status=404)

    def do_POST(self):
        _touch_activity()
        route, _, query = self.path.partition("?")
        if route == "/shutdown":
            self._handle_shutdown(query)
        elif self.path == "/convert":
            self._handle_convert()
        elif self.path == "/explode":
            self._handle_explode_alias()
        elif self.path == "/progress":
            self._handle_progress()
        elif self.path == "/context":
            self._handle_context()
        elif self.path == "/debug":
            self._handle_debug()
        elif self.path == "/clipboard":
            self._handle_clipboard()
        elif self.path == "/trigger":
            self._handle_trigger()
        elif self.path == "/pending":
            self._handle_pending_post()
        elif self.path == "/webviewer/start":
            self._handle_webviewer_start()
        elif self.path == "/webviewer/stop":
            self._handle_webviewer_stop()
        elif self.path == "/webviewer/push":
            self._handle_webviewer_push()
        elif self.path == "/lint":
            self._handle_lint()
        elif self.path.startswith("/preview/"):
            layout_name = self.path[len("/preview/"):]
            self._handle_preview_post(layout_name)
        elif self.path.startswith("/plugin/"):
            self._handle_plugin_proxy("POST", self.path[len("/plugin/"):])
        else:
            self._send_json({"error": "Not found"}, status=404)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _handle_health(self):
        self._send_json(_health(getattr(self.server, "server_address", None)))

    def _handle_shutdown(self, query: str = ""):
        """POST /shutdown — stop the companion through the same path as Ctrl-C (roadmap P3-20).

        Needs the state file's token in X-Companion-Token, so a web page can't stop the companion
        with a blind localhost request. Body {"force": bool, "wait": bool}, or ?force=1&wait=1.
        """
        global _stopping, _shutdown_reason
        # Read the body before any reply: closing with unread data makes Windows reset the
        # connection, and the client then gets ConnectionAbortedError instead of the 403
        try:
            raw = self._read_body()
        except OSError as exc:
            self._send_json({"error": f"Invalid request: {exc}"}, status=400)
            return
        sent = self.headers.get(companion_state.TOKEN_HEADER) or ""
        if not _state_token or not hmac.compare_digest(sent.encode("utf-8", "replace"),
                                                       _state_token.encode("utf-8")):
            log.warning("POST /shutdown from %s refused: missing or wrong %s.",
                        self.address_string(), companion_state.TOKEN_HEADER)
            self._send_json({"error": f"Missing or wrong {companion_state.TOKEN_HEADER} header: send the "
                                      "token from the companion's state file (`companion_server.py stop` "
                                      "does this)."}, status=403)
            return
        try:
            payload = json.loads(raw or b"{}")
        except ValueError as exc:
            self._send_json({"error": f"Invalid request: {exc}"}, status=400)
            return
        payload = payload if isinstance(payload, dict) else {}
        params = urllib.parse.parse_qs(query, keep_blank_values=True)

        def flag(name: str) -> bool:
            values = params.get(name) or []
            from_query = bool(values) and values[-1].strip().lower() in ("1", "true", "yes", "on", "")
            return bool(payload.get(name)) or from_query

        force, wait = flag("force"), flag("wait")
        job = _active_convert_job()
        server = self.server
        if job is not None and not force and not wait:
            self._send_json({"error": "conversion running", "job": job}, status=409)
            return
        _stopping = True
        if job is not None and not force:
            log.info("POST /shutdown from %s: stopping after conversion %s.",
                     self.address_string(), job["job_id"])
            self._send_json({"status": "stopping after job", "job": job}, status=202)
            threading.Thread(target=_shutdown_when_idle, args=(server,), daemon=True,
                             name="shutdown-after-job").start()
            return
        if job is not None:
            _shutdown_reason = "stopped by /shutdown"
        log.info("POST /shutdown from %s: stopping%s.", self.address_string(),
                 " (forced: the running conversion is stopped)" if job is not None else "")
        self._send_json({"status": "stopping"}, status=202)
        # serve_forever runs in main(); shutdown() from here makes it return into main()'s finally
        threading.Thread(target=server.shutdown, daemon=True, name="shutdown").start()

    def _handle_plugin_proxy(self, method: str, subpath: str):
        """Thin pass-through to the plug-in's HTTP server, bearer token injected.

        Optional convenience for constrained environments (e.g. a container that
        can reach the companion but not the plug-in's port) and to keep a single
        base URL. Direct plug-in access is the baseline; this proxy is inert
        until explicitly called. Refuses with 502 when the plug-in is not usable,
        so the agent's fallback stays self-enforcing.
        """
        block = _check_plugin()
        if not block.get("usable"):
            self._send_json(
                {"success": False, "error": "Plug-in not usable",
                 "plugin": {"installed": block.get("installed", False), "usable": False}},
                status=502,
            )
            return

        server = block.get("server", {})
        base = server.get("base", "").rstrip("/")
        token = server.get("token", "")
        url = f"{base}/{subpath}"

        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        body = None
        if method == "POST":
            body = self._read_body()
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")

        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                status = resp.status
                ctype = resp.headers.get("Content-Type", "application/json; charset=utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
            ctype = exc.headers.get("Content-Type", "application/json; charset=utf-8")
        except Exception as exc:
            log.warning("Plug-in proxy to %s failed: %s", url, exc)
            self._send_json({"success": False, "error": f"Proxy failed: {exc}"}, status=502)
            return

        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _handle_convert(self, payload: "dict | None" = None, extra: "dict | None" = None):
        """POST /convert — queue an fm-saxml conversion and return its job ID at once."""
        if payload is None:
            try:
                payload = json.loads(self._read_body() or b"{}")
            except (ValueError, OSError) as exc:
                self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
                return
        extra = extra or {}
        if _stopping:
            self._send_json({"success": False, "error": "The companion is stopping (POST /shutdown); "
                                                        "start it again and resend.", **extra}, status=503)
            return
        if not isinstance(payload, dict):
            self._send_json({"success": False, "error": "Request body must be a JSON object", **extra},
                            status=400)
            return

        missing = [f for f in ("solution_name", "export_path") if not payload.get(f)]
        if missing:
            self._send_json(
                {"success": False, "error": f"Missing required fields: {', '.join(missing)}", **extra},
                status=400,
            )
            return

        switches = {name: payload.get(name, False) for name in CONVERT_SWITCHES}
        wrong = [name for name, value in switches.items() if not isinstance(value, bool)]
        if wrong:
            self._send_json(
                {"success": False, "error": f"{', '.join(wrong)} must be true or false", **extra},
                status=400,
            )
            return
        switches["html"] = switches["html"] or switches["webmcp"]    # as in fm-saxml: --webmcp turns on --html

        solution_name = str(payload["solution_name"]).strip()
        export_path = _incoming_export_path(str(payload["export_path"]), solution_name)
        if not export_path:
            self._send_json({"success": False, "error": _unresolved_export_path_error("export_path"), **extra},
                            status=400)
            return
        if not os.path.exists(export_path):
            self._send_json({"success": False, "error": f"export_path not found: {export_path}", **extra},
                            status=400)
            return

        docs_root, docs_root_source = _resolve_docs_root(payload.get("docs_root") or "")
        if not docs_root:
            self._send_json({"success": False, "error": _no_docs_root_error(), **extra}, status=400)
            return

        job, running = _start_convert_job(solution_name, export_path, docs_root, **switches)
        if job is None:
            self._send_json(
                {"success": False,
                 "error": f"{solution_name!r} is already converting (job {running['job_id']})",
                 "job_id": running["job_id"], "state": running["state"],
                 "status_url": f"/convert/{running['job_id']}", **extra},
                status=409,
            )
            return
        log.info("convert %s queued: solution=%r export=%r docs_root=%r (%s) html=%s webmcp=%s",
                 job["job_id"], solution_name, export_path, docs_root, DOCS_ROOT_SOURCES[docs_root_source],
                 job["html"], job["webmcp"])
        self._send_json(
            {"success": True, "job_id": job["job_id"], "state": job["state"],
             "status_url": f"/convert/{job['job_id']}", **extra},
            status=202,
        )

    def _handle_convert_get(self, job_id: str):
        """GET /convert/<job id> — a conversion's state and progress, then its counts and warnings."""
        job = _get_convert_job(job_id)
        if job is None:
            self._send_json({"success": False, "error": f"No such job: {job_id}"}, status=404)
            return
        self._send_json({"success": True, **job})

    def _handle_explode_alias(self):
        """POST /explode — deprecated alias for POST /convert (kept for one release)."""
        notice = "POST /explode is deprecated: use POST /convert, which runs fm-saxml as a background job."
        log.warning("%s (request from %s)", notice, self.address_string())
        extra = {"deprecated": True, "deprecation": notice}
        try:
            payload = json.loads(self._read_body() or b"{}")
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}", **extra}, status=400)
            return
        if isinstance(payload, dict) and not payload.get("export_path"):
            payload["export_path"] = payload.get("export_file_path", "")
        self._handle_convert(payload, extra)

    # The fmparse.sh / fm-xml-export-exploder runner that POST /explode used to call.
    # No route reaches it any more (/explode is an alias of /convert); it is removed,
    # together with fmparse.sh, by roadmap unit P3-09.
    def _handle_explode_legacy(self):
        # Read and parse request body
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json(
                {"success": False, "exit_code": -1, "error": f"Invalid request: {exc}"},
                status=400,
            )
            return

        # Validate required fields
        missing = [
            f for f in ("solution_name", "export_file_path", "repo_path")
            if not payload.get(f)
        ]
        if missing:
            self._send_json(
                {
                    "success": False,
                    "exit_code": -1,
                    "error": f"Missing required fields: {', '.join(missing)}",
                },
                status=400,
            )
            return

        solution_name = payload["solution_name"]
        export_file_path = payload["export_file_path"]
        repo_path = payload["repo_path"]
        exploder_bin_path = payload.get("exploder_bin_path", "")

        # Expand ~ in paths
        repo_path = os.path.expanduser(repo_path)
        export_file_path = os.path.expanduser(export_file_path)

        # Under WSL2, translate Windows-POSIX paths (/C:/...) to mount points
        # (/mnt/c/...). FileMaker on Windows uses ConvertFromFileMakerPath(...; 1)
        # which produces paths like /C:/Users/... that are invalid in WSL2.
        # On native Windows and macOS the paths are used unchanged.
        export_file_path = _incoming_export_path(export_file_path, solution_name)
        repo_path = _translate_incoming_path(repo_path)

        # If repo_path is "?" it means FileMaker's ConvertFromFileMakerPath failed —
        # this happens on WSL2 when the repo lives on the WSL filesystem and FileMaker
        # on Windows cannot convert the \\wsl.localhost\... UNC path to a POSIX path.
        # Fall back to repo_path_override from agent/config/automation.json.
        if repo_path == "?":
            override = _automation_config.get("repo_path_override", "")
            if override:
                repo_path = os.path.expanduser(override)
                log.info(
                    "repo_path was '?' — using repo_path_override from automation.json: %r",
                    repo_path,
                )
            else:
                self._send_json(
                    {
                        "success": False,
                        "exit_code": -1,
                        "error": (
                            "repo_path is '?' — FileMaker could not convert the repo path to POSIX "
                            "(common on WSL2 when the repo is on the WSL filesystem). "
                            "Add 'repo_path_override' to agent/config/automation.json with the "
                            "absolute POSIX path to your agentic-fm folder (e.g. /home/user/agentic-fm). "
                            "See agent/docs/SANDBOXED_ENVIRONMENT.md for details."
                        ),
                    },
                    status=400,
                )
                return

        # An export path of "?" that _incoming_export_path could not rebuild.
        if not export_file_path:
            self._send_json(
                {
                    "success": False,
                    "exit_code": -1,
                    "error": _unresolved_export_path_error("export_file_path"),
                },
                status=400,
            )
            return

        # Build environment for subprocess
        env = os.environ.copy()
        if exploder_bin_path:
            env["FM_XML_EXPLODER_BIN"] = os.path.expanduser(exploder_bin_path)

        # Build command: {repo_path}/fmparse.sh -s "{solution_name}" "{export_file_path}"
        fmparse = os.path.join(repo_path, "fmparse.sh")
        cmd = [fmparse, "-s", solution_name, export_file_path]

        log.info(
            "Running fmparse.sh: solution=%r export=%r cwd=%r",
            solution_name,
            export_file_path,
            repo_path,
        )

        try:
            result = _run_command_streaming(
                cmd,
                cwd=repo_path,
                env=env,
                label="fmparse.sh",
            )

            success = result["returncode"] == 0
            response = {
                "success": success,
                "exit_code": result["returncode"],
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            }
            status = 200 if success else 500

            log.info(
                "fmparse.sh exited with code %d", result["returncode"]
            )

        except Exception as exc:
            log.exception("Exception running fmparse.sh: %s", exc)
            response = {
                "success": False,
                "exit_code": -1,
                "error": str(exc),
            }
            status = 500

        self._send_json(response, status=status)

    def _handle_progress(self):
        """POST /progress — record one export step event from FileMaker's export script."""
        try:
            payload = json.loads(self._read_body() or b"{}")
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return
        event, error = _validate_progress(payload)
        if error:
            self._send_json({"success": False, "error": error}, status=400)
            return
        state = _record_export_progress(event)
        self._send_json({"success": True, "solution_name": state["solution_name"], "event": state["event"],
                         "index": state["index"], "total": state["total"], "done": state["done"]})

    def _handle_context(self):
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        missing = [f for f in ("repo_path", "context") if not payload.get(f)]
        if missing:
            self._send_json(
                {"success": False, "error": f"Missing required fields: {', '.join(missing)}"},
                status=400,
            )
            return

        repo_path = os.path.expanduser(payload["repo_path"])
        context = payload["context"]

        # Accept context as a pre-serialised string or a parsed object
        if isinstance(context, str):
            try:
                json.loads(context)  # validate only
            except ValueError as exc:
                self._send_json({"success": False, "error": f"Invalid context JSON: {exc}"}, status=400)
                return
            context_str = context
        else:
            context_str = json.dumps(context, indent=2, ensure_ascii=False)

        output_path = os.path.join(repo_path, "agent", "CONTEXT.json")

        # Check context_version and warn if outdated
        CONTEXT_VERSION_CURRENT = 2
        try:
            ctx_data = json.loads(context_str) if isinstance(context_str, str) else context
            ctx_version = ctx_data.get("context_version")
            if ctx_version is None or ctx_version < CONTEXT_VERSION_CURRENT:
                log.warning(
                    "CONTEXT.json has context_version=%s (current is %s). "
                    "Update the Context() custom function in your solution.",
                    ctx_version, CONTEXT_VERSION_CURRENT,
                )
        except (ValueError, TypeError, AttributeError):
            pass

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(context_str)
            log.info("CONTEXT.json written to %s", output_path)
            self._send_json({"success": True, "path": output_path})
        except Exception as exc:
            log.exception("Failed to write CONTEXT.json: %s", exc)
            self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_pending_get(self):
        """Return and clear the pending paste job."""
        global _pending_job
        with _pending_lock:
            job = _pending_job.copy()
            _pending_job = {}
        self._send_json(job)

    def _handle_pending_post(self):
        """Set the pending paste job."""
        global _pending_job
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return
        target = payload.get("target", "")
        auto_save = bool(payload.get("auto_save", False))
        select_all = bool(payload.get("select_all", True))
        with _pending_lock:
            _pending_job = {"target": target, "auto_save": auto_save, "select_all": select_all}
        log.info("Pending job set: target=%r auto_save=%s select_all=%s", target, auto_save, select_all)
        self._send_json({"success": True})

    def _handle_trigger(self):
        """
        Trigger FM Pro to perform a named script, through an fmp:// URL or osascript.

        Payload: { "script": "name", "parameter": "...", "target_file": "...",
                   "backend": "url" | "applescript", "fm_app_name": "FileMaker Pro — ..." }
        Returns: { "success": bool, "backend": str, ... } — "url" for the url
        backend, "stdout" and "stderr" for AppleScript.
        """
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        fm_app = payload.get("fm_app_name", "FileMaker Pro")
        script = payload.get("script", "")
        parameter = payload.get("parameter", "")
        target_file = payload.get("target_file", "")

        # raw_applescript bypasses the FM do script path — no script name required
        raw = payload.get("raw_applescript", "")
        backend = payload.get("backend") or ("applescript" if raw else _default_trigger_backend())
        if backend not in TRIGGER_BACKENDS:
            self._send_json({"success": False, "error": f"Unknown backend: {backend!r} (use url or applescript)"},
                            status=400)
            return
        if raw and backend != "applescript":
            self._send_json({"success": False, "error": "raw_applescript needs the applescript backend"}, status=400)
            return
        if not raw and not script:
            self._send_json({"success": False, "error": "Missing required field: script"}, status=400)
            return
        if backend == "url" and not target_file:
            self._send_json({"success": False, "error": "The url backend needs target_file (the open file's name)"},
                            status=400)
            return

        if not raw and parameter:
            # Store the target and auto_save flag in the pending slot so the
            # FM script can retrieve them via GET /pending (AppleScript parameter
            # passing via "given parameter:" is unreliable in FM Pro 22).
            # The url backend also passes the parameter itself.
            global _pending_job
            auto_save = bool(payload.get("auto_save", False))
            select_all = bool(payload.get("select_all", True))
            with _pending_lock:
                _pending_job = {"target": parameter, "auto_save": auto_save, "select_all": select_all}
            log.info("Pending job set: target=%r auto_save=%s select_all=%s", parameter, auto_save, select_all)

        if backend == "url":
            url = fmp_script_url(target_file, script, parameter)
            try:
                _open_fmp_url(url)
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.error("Trigger failed: could not open %s: %s", url, exc)
                self._send_json({"success": False, "backend": "url", "url": url,
                                 "error": f"Could not open the fmp:// URL: {exc}"}, status=500)
                return
            log.info("Trigger: sent '%s' in %s to FileMaker as an fmp:// URL", script, target_file)
            self._send_json({"success": True, "backend": "url", "url": url})
            return

        def as_str(s):
            """Escape double-quotes for use inside an AppleScript double-quoted string."""
            return s.replace("\\", "\\\\").replace('"', '\\"')

        if raw:
            applescript = raw
        else:
            # When target_file is provided, address the specific FM document
            # by name instead of positional document 1. This ensures the
            # correct file is targeted when multiple files are open.
            if target_file:
                doc_clause = f'tell (first document whose name contains "{as_str(target_file)}")'
                log.info("Trigger: targeting document %r", target_file)
            else:
                doc_clause = "tell document 1"
                log.info("Trigger: no target_file — using document 1")

            applescript = (
                f'tell application "{as_str(fm_app)}"\n'
                f'    activate\n'
                f'    {doc_clause}\n'
                f'        do script "{as_str(script)}"\n'
                f'    end tell\n'
                f'end tell'
            )

        try:
            result = subprocess.run(
                ["osascript", "-e", applescript],
                capture_output=True, text=True, timeout=30
            )
            success = result.returncode == 0
            if success:
                log.info("Trigger: ran '%s' in %s", script, fm_app)
            else:
                log.error("Trigger failed: %s", result.stderr)
            self._send_json({
                "success": success,
                "backend": "applescript",
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            })
        except subprocess.TimeoutExpired:
            self._send_json({"success": False, "error": "osascript timed out after 30s"}, status=500)
        except FileNotFoundError:
            self._send_json({"success": False, "error": "osascript not found — is this macOS?"}, status=500)
        except Exception as exc:
            log.exception("Trigger handler error: %s", exc)
            self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_clipboard_read(self):
        """Read FM objects from the clipboard with clipboard.py (macOS or Windows) and return as XML."""
        try:
            result = subprocess.run(
                [sys.executable, _clipboard_script(), "read"],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
            )
            if result.returncode == 0 and result.stdout.strip():
                log.info("Clipboard read succeeded")
                self._send_json({"success": True, "xml": result.stdout})
            else:
                error = result.stderr.strip() or "No FileMaker objects on clipboard"
                log.warning("Clipboard read failed: %s", error)
                self._send_json(
                    {"success": False, "error": error},
                    status=404 if "No FileMaker" in error else 500,
                )
        except subprocess.TimeoutExpired:
            self._send_json(
                {"success": False, "error": "Clipboard read timed out"},
                status=500,
            )
        except Exception as exc:
            log.exception("Clipboard read error: %s", exc)
            self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_clipboard(self):
        """Accept XML content and write it to the clipboard with clipboard.py (macOS or Windows)."""
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        xml = payload.get("xml", "")
        if not xml:
            self._send_json({"success": False, "error": "Missing required field: xml"}, status=400)
            return

        import tempfile

        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".xml", encoding="utf-8", delete=False
            ) as tmp:
                tmp.write(xml)
                tmp_path = tmp.name

            result = subprocess.run(
                [sys.executable, _clipboard_script(), "write", tmp_path],
                capture_output=True, text=True, encoding="utf-8", errors="replace"
            )
            os.unlink(tmp_path)

            if result.returncode == 0:
                log.info("Clipboard write succeeded")
                self._send_json({"success": True})
            else:
                log.error("Clipboard write failed: %s", result.stderr)
                self._send_json(
                    {"success": False, "error": result.stderr or "clipboard script returned non-zero"},
                    status=500,
                )
        except Exception as exc:
            log.exception("Clipboard handler error: %s", exc)
            self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_debug(self):
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        # Resolve repo root from script location
        script_dir = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(script_dir))
        debug_dir = os.path.join(repo_root, "agent", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        output_path = os.path.join(debug_dir, "output.json")

        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)

        log.info("Debug output written to %s", output_path)
        self._send_json({"success": True, "path": output_path})

    def _handle_webviewer_status(self):
        global _webviewer_proc
        with _webviewer_lock:
            running = _webviewer_proc is not None and _webviewer_proc.poll() is None
        self._send_json({"running": running})

    def _handle_webviewer_start(self):
        global _webviewer_proc
        try:
            body = self._read_body()
            payload = json.loads(body) if body else {}
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        repo_path = payload.get("repo_path", "")
        if not repo_path:
            self._send_json({"success": False, "error": "Missing required field: repo_path"}, status=400)
            return

        repo_path = os.path.expanduser(repo_path)
        webviewer_path = os.path.join(repo_path, "webviewer")

        with _webviewer_lock:
            if _webviewer_proc is not None and _webviewer_proc.poll() is None:
                self._send_json({"success": True, "status": "already_running"})
                return

            try:
                proc = subprocess.Popen(
                    ["npm", "run", "dev"],
                    cwd=webviewer_path,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                _webviewer_proc = proc
                log.info("Started webviewer (pid=%d) in %s", proc.pid, webviewer_path)
                self._send_json({"success": True, "status": "started", "pid": proc.pid})
            except Exception as exc:
                log.exception("Failed to start webviewer: %s", exc)
                self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_webviewer_stop(self):
        global _webviewer_proc
        with _webviewer_lock:
            if _webviewer_proc is None or _webviewer_proc.poll() is not None:
                self._send_json({"success": True, "status": "not_running"})
                return

            try:
                what = _stop_process_tree(_webviewer_proc)
                _webviewer_proc = None
                log.info("Stopped webviewer (%s)", what)
                self._send_json({"success": True, "status": "stopped"})
            except Exception as exc:
                log.exception("Failed to stop webviewer: %s", exc)
                self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_webviewer_push(self):
        """
        Write an agent output payload for the webviewer to pick up via polling.

        Payload: { "type": "preview"|"diff"|"result"|"diagram"|"layout-preview", "content": "...", "before": "...", "styles": "...", "repo_path": "..." }
        Returns: { "success": bool }
        """
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        payload_type = payload.get("type", "")
        if payload_type not in ("preview", "diff", "result", "diagram", "layout-preview"):
            self._send_json({"success": False, "error": f"Unknown type: {payload_type!r}. Must be preview, diff, result, diagram, or layout-preview."}, status=400)
            return

        repo_path = payload.get("repo_path", "")
        if not repo_path:
            self._send_json({"success": False, "error": "Missing required field: repo_path"}, status=400)
            return

        repo_path = os.path.expanduser(repo_path)
        output_path = os.path.join(repo_path, "agent", "config", ".agent-output.json")

        import time
        output = {
            "type": payload_type,
            "content": payload.get("content", ""),
            "before": payload.get("before", ""),
            "timestamp": time.time(),
        }
        # Include optional styles field for layout-preview payloads
        if payload.get("styles"):
            output["styles"] = payload["styles"]

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            log.info("Agent output written to %s (type=%s)", output_path, payload_type)
            self._send_json({"success": True, "path": output_path})
        except Exception as exc:
            log.exception("Failed to write agent output: %s", exc)
            self._send_json({"success": False, "error": str(exc)}, status=500)

    def _handle_preview_get(self, layout_name: str):
        """
        Serve a layout HTML preview.

        GET /preview/<layout_name>
        Returns: text/html — the preview file, a fallback from .agent-output.json,
                 or a placeholder message.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(here))

        # Look for agent/sandbox/{layout_name}_web.html
        html_path = os.path.join(repo_root, "agent", "sandbox", f"{layout_name}_web.html")
        if os.path.isfile(html_path):
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    content = f.read()
                self._send_html(content)
                return
            except OSError as exc:
                log.warning("Could not read preview file %s: %s", html_path, exc)

        # Fall back to agent/config/.agent-output.json
        output_json_path = os.path.join(repo_root, "agent", "config", ".agent-output.json")
        if os.path.isfile(output_json_path):
            try:
                with open(output_json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                content = data.get("content", "")
                if content:
                    self._send_html(content)
                    return
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Could not read .agent-output.json: %s", exc)

        # Placeholder
        self._send_html(f"<h2>No preview available for {layout_name}</h2>")

    def _handle_preview_post(self, layout_name: str):
        """
        Store a layout HTML preview.

        POST /preview/<layout_name>
        Body: { "html": "...", "solution": "..." }
        Returns: { "success": true, "path": "agent/sandbox/{layout_name}_web.html" }
        """
        try:
            body = self._read_body()
            payload = json.loads(body)
        except (ValueError, OSError) as exc:
            self._send_json({"success": False, "error": f"Invalid request: {exc}"}, status=400)
            return

        html = payload.get("html", "")
        if not html:
            self._send_json({"success": False, "error": "Missing required field: html"}, status=400)
            return

        here = os.path.dirname(os.path.abspath(__file__))
        repo_root = os.path.dirname(os.path.dirname(here))
        sandbox_dir = os.path.join(repo_root, "agent", "sandbox")
        out_path = os.path.join(sandbox_dir, f"{layout_name}_web.html")
        rel_path = f"agent/sandbox/{layout_name}_web.html"

        try:
            os.makedirs(sandbox_dir, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(html)
            log.info("Preview written to %s", out_path)
            self._send_json({"success": True, "path": rel_path})
        except Exception as exc:
            log.exception("Failed to write preview: %s", exc)
            self._send_json({"success": False, "error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _handle_lint(self):
        """Lint FileMaker code via FMLint engine.

        POST /lint
        Body: { "content": "...", "format": "xml"|"hr"|null, "tier": 1|2|3|null,
                "disable": ["N003", ...] }
        Returns: LintResult as JSON
        """
        try:
            body = json.loads(self._read_body())
        except (json.JSONDecodeError, ValueError):
            self._send_json({"error": "Invalid JSON body"}, status=400)
            return

        content = body.get("content", "")
        if not content:
            self._send_json({"error": "Missing 'content' field"}, status=400)
            return

        fmt = body.get("format")
        tier = body.get("tier")
        disabled = body.get("disable", [])

        try:
            # Resolve project root from this script's location
            here = os.path.dirname(os.path.abspath(__file__))
            project_root = os.path.join(here, "..", "..")

            sys.path.insert(0, project_root)
            from agent.fmlint import lint

            config = {}
            if disabled:
                config["disable"] = disabled
            if tier is not None:
                config["max_tier"] = tier

            result = lint(
                content,
                fmt=fmt,
                project_root=project_root,
                config=config,
            )
            self._send_json(result.to_dict())
        except Exception as e:
            logging.exception("FMLint error")
            self._send_json({"error": str(e)}, status=500)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length > 0 else b""

    def _send_json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, content: str, status: int = 200):
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _check_for_updates():
    """Fetch the remote version.txt and warn if a newer version is available."""
    try:
        with urllib.request.urlopen(REMOTE_VERSION_URL, timeout=5) as resp:
            remote = resp.read().decode("utf-8").strip()
        if remote and remote != VERSION:
            local_parts = tuple(int(x) for x in VERSION.split(".") if x.isdigit())
            remote_parts = tuple(int(x) for x in remote.split(".") if x.isdigit())
            if remote_parts > local_parts:
                log.warning(
                    "A new version is available: v%s (you have v%s). "
                    "Run 'git pull --ff-only' in your agentic-fm folder to update, "
                    "then restart the server. See UPDATES.md for details.",
                    remote,
                    VERSION,
                )
    except Exception:
        pass  # No network, rate-limited, etc. — fail silently


def _first_int(*candidates) -> int:
    """Return the first candidate coercible to int, skipping None/'' and bad values."""
    for c in candidates:
        if c is None or c == "":
            continue
        try:
            return int(c)
        except (TypeError, ValueError):
            continue
    return 0


def _load_companion_config() -> dict:
    """Read the `companion` block from agent/config/companion.json.

    Fail-open: an absent file returns {} silently (the common case); an unreadable
    or malformed file warns and returns {}. Either way built-in defaults apply and
    the server still boots. Mirrors deploy.py's _load_config discipline.
    """
    try:
        with open(COMPANION_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        log.warning("Could not read companion.json (%s) — using built-in defaults.", exc)
        return {}
    comp = data.get("companion") if isinstance(data, dict) else None
    return comp if isinstance(comp, dict) else {}


COMMANDS = ("start", "status", "stop", "restart", "attach")
DEFAULT_STOP_TIMEOUT = 15.0
# cmd_stop exit codes after which restart goes on to start here: 0 = stopped, or nothing was running
# (1 = could not stop it, 2 = refused because a conversion runs: restart exits with that code)
RESTART_STOP_OK = (0,)


def parse_args(argv: "list | None" = None):
    parser = argparse.ArgumentParser(
        description="agentic-fm companion server — runs fm-saxml and other local tools over HTTP for FileMaker.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=COMMANDS,
        default="start",
        help=(
            "start (the default): run the companion in this terminal; if one already runs on the "
            "port, show it and follow its log. status: say whether one runs (exit 0) or not (exit 1). "
            "stop: stop it cleanly. restart: stop, then start here. attach: follow a running "
            "companion's log; Ctrl-C asks whether to detach or stop it."
        ),
    )
    # Defaults are None so an unset flag falls through to env/companion.json/default
    # in main()'s resolution; passing the flag pins the value at the top of the chain.
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Port to listen on. Overrides COMPANION_PORT and companion.json "
            f"(default: {DEFAULT_PORT})."
        ),
    )
    parser.add_argument(
        "--idle-timeout",
        type=int,
        default=None,
        help=(
            "Seconds of inactivity before the server shuts itself down. "
            "0 disables auto-shutdown (always-on). Overrides COMPANION_IDLE_TIMEOUT "
            f"and companion.json (default: {DEFAULT_IDLE_TIMEOUT})."
        ),
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help=(
            "Print plain log lines only, without the live progress panel "
            "(same as COMPANION_NO_LIVE=1). The panel needs a terminal and Rich."
        ),
    )
    parser.add_argument(
        "--log-file",
        default=None,
        metavar="PATH",
        help=(
            f"Log file (debug lines, rotated by size). Overrides {LOG_FILE_ENV} and companion.json "
            "log_file (default: companion-server.log in the per-user state folder)."
        ),
    )
    parser.add_argument("--no-log-file", action="store_true", help="Write no log file (console only).")
    parser.add_argument(
        "--replace",
        action="store_true",
        help="start: if a companion already runs on the port, stop it and start here instead.",
    )
    parser.add_argument(
        "--no-attach",
        action="store_true",
        help="start: if a companion already runs on the port, print its details and exit 1.",
    )
    parser.add_argument(
        "--launched-by",
        default=None,
        metavar="WHO",
        help=(
            f"Who started the companion, shown by /health and status: {', '.join(LAUNCHED_BY_CHOICES)} "
            f"(default: ${LAUNCHED_BY_ENV}, else terminal)."
        ),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_STOP_TIMEOUT,
        metavar="SECONDS",
        help=f"stop, restart: seconds to wait for the companion to exit (default: {DEFAULT_STOP_TIMEOUT:g}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="stop, restart: stop even while a conversion runs (the conversion is recorded as failed).",
    )
    return parser.parse_args(argv)


def _stop_process_tree(proc) -> str:
    """Stop a process started in its own session/group, with its children. Returns what was done.

    POSIX: SIGTERM to its process group. Windows has no process groups to signal (no os.getpgid or
    os.killpg): taskkill /T stops the tree, and terminate() is the fallback.
    """
    if sys.platform == "win32":
        try:
            done = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                  capture_output=True, timeout=15)
            if done.returncode == 0:
                return f"process tree {proc.pid}"
        except (OSError, subprocess.SubprocessError):
            pass
        proc.terminate()
        return f"process {proc.pid}"
    pgid = os.getpgid(proc.pid)
    os.killpg(pgid, signal.SIGTERM)
    return f"process group {pgid}"


def _shutdown_cleanup(server):
    server.server_close()
    _stop_viewer_servers()
    with _webviewer_lock:
        if _webviewer_proc is not None and _webviewer_proc.poll() is None:
            try:
                log.info("Stopped webviewer (%s)", _stop_process_tree(_webviewer_proc))
            except Exception as exc:
                log.warning("Failed to stop webviewer on shutdown: %s", exc)


def _convert_jobs_snapshot() -> list:
    with _convert_lock:
        return json.loads(json.dumps(list(_convert_jobs.values())))


def _start_live_panel(no_live: bool):
    """Start the live progress panel (companion_live.py) when stdout is a terminal and Rich imports.

    Returns the LiveView to stop with _stop_live_panel, or None when the companion prints plain
    log lines. While the panel is on, file_start lines are logged at debug (see _live_panel_on).
    """
    global _live_panel_on
    try:
        import companion_live
    except ImportError as exc:
        log.info("Live progress panel: off (%s); plain log lines only.", exc)
        return None
    try:
        live, message = companion_live.start_live(_get_export_progress, _convert_jobs_snapshot,
                                                  no_live=no_live, stage_labels=CONVERT_STAGE_LABELS)
    except Exception as exc:    # the panel is a nicety: it must never stop the companion
        log.warning("Live progress panel failed to start (%s); plain log lines only.", exc)
        return None
    _live_panel_on = live is not None
    log.info(message)
    return live


def _stop_live_panel(live) -> None:
    """Stop the panel from _start_live_panel; file_start lines go back to info."""
    global _live_panel_on
    _live_panel_on = False
    if live is not None:
        live.stop()


def main(argv: "list | None" = None):
    global _stopping, _shutdown_reason
    args = parse_args(argv)
    comp = _load_companion_config()

    # Server-bind resolution (highest wins): CLI flag / env var → companion.json → default.
    bind_host = (
        os.environ.get("COMPANION_BIND_HOST")
        or comp.get("bind_host")
        or DEFAULT_BIND_HOST
    )
    port = _first_int(
        args.port, os.environ.get("COMPANION_PORT"), comp.get("port"), DEFAULT_PORT
    )
    idle_timeout = _first_int(
        args.idle_timeout, os.environ.get("COMPANION_IDLE_TIMEOUT"),
        comp.get("idle_timeout_seconds"), DEFAULT_IDLE_TIMEOUT,
    )
    advertise_host = comp.get("advertise_host") or DEFAULT_ADVERTISE_HOST
    client_host = _client_host(bind_host)

    # status, stop and attach talk to the running companion (companion_control); restart stops it first
    if args.command == "status":
        sys.exit(companion_control.cmd_status(client_host, port))
    if args.command == "stop":
        sys.exit(companion_control.cmd_stop(client_host, port, timeout=args.timeout, force=args.force))
    if args.command == "attach":
        sys.exit(companion_control.cmd_attach(client_host, port))
    if args.command == "restart":
        code = companion_control.cmd_stop(client_host, port, timeout=args.timeout, force=args.force)
        if code not in RESTART_STOP_OK:
            sys.exit(code)

    server = _bind_server(bind_host, port)
    if server is None:
        # P3-21: show (and by default follow) the companion already there, or name what holds the port
        code = companion_control.handle_port_clash(client_host, port, replace=args.replace,
                                                   no_attach=args.no_attach)
        if code is not None:
            sys.exit(code)
        server = _bind_server(bind_host, port)    # --replace stopped the other one
        if server is None:
            log.error("Port %d on %s is still in use after stopping the companion there; "
                      "try again, or start this one with --port.", port, bind_host)
            sys.exit(1)
    port = server.server_address[1]     # the real port when --port 0 asked for any free one

    _stopping, _shutdown_reason = False, ""
    log_file, log_source = _resolve_log_file(args.log_file, args.no_log_file, comp)
    if not _setup_file_logging(log_file):
        log_file = ""
    _init_server_info(bind_host, port, log_file, _launched_by(args.launched_by))
    state_path = _write_state_file()
    companion_state.set_terminal_title(f"agentic-fm companion :{port} (PID {os.getpid()})")

    log.info("companion_server v%s listening on %s:%d (PID %d)", VERSION, bind_host, port, os.getpid())
    log.info("Clients reach it at http://%s:%d (advertise_host).", advertise_host, port)
    if log_file:
        log.info("Log file: %s (debug lines; %s)", log_file,
                 "default" if log_source == "default" else f"from {log_source}")
    else:
        log.info("Log file: off (%s)", log_source or "could not open it")
    if state_path:
        log.debug("State file: %s", state_path)
    threading.Thread(target=_check_for_updates, daemon=True).start()
    if idle_timeout > 0:
        log.info("Idle auto-shutdown: %ds with no requests.", idle_timeout)
        threading.Thread(target=_idle_watchdog, args=(server, idle_timeout), daemon=True).start()
    else:
        log.info("Idle auto-shutdown: disabled (always-on).")
    log.info("Endpoints: GET /health  GET /clipboard  GET /webviewer/status  GET /preview/<name>  GET /convert/<job id>  GET|POST /plugin/<path>  POST /shutdown  POST /convert  POST /explode (deprecated alias of /convert)  POST /progress  POST /context  POST /clipboard  POST /trigger  POST /debug  POST /webviewer/start  POST /webviewer/stop  POST /webviewer/push  POST /preview/<name>")
    log.info(_docs_root_startup_message())
    live = None
    previous_signals = _install_stop_signals(server)
    try:
        live = _start_live_panel(args.no_live)
        log.info("Press Ctrl-C to stop (or run `companion_server.py stop` from another terminal).")
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down.")
    finally:
        # Ctrl-C, POST /shutdown and the idle watchdog all end here
        _stop_live_panel(live)
        if _convert_job_active():
            _end_active_jobs(_shutdown_reason or "stopped: the companion shut down")
        _shutdown_cleanup(server)
        try:
            companion_state.remove_state(pid=os.getpid())
        except OSError as exc:
            log.warning("Could not remove the state file: %s", exc)
        _restore_signals(previous_signals)
        log.info("Stopped.")
        _teardown_file_logging()


if __name__ == "__main__":
    # Windows pipes and redirects default to the ANSI code page: accented names would garble
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
    main()
