# Companion Server

The companion server is a lightweight local HTTP server that runs host-side tools for FileMaker and the agent: fm-saxml conversion, context and debug output, the clipboard, script triggering, linting, the webviewer and layout previews. FileMaker calls it via the native **Insert from URL** script step, which functions as a curl-compatible HTTP client.

**Why this exists:** FileMaker has no built-in mechanism to execute arbitrary shell commands. The companion server fills this gap — no third-party plugin is required. As long as the server is running on the developer's machine, FileMaker can trigger XML conversion and context generation through a simple HTTP POST.

**Platforms:** The companion runs on macOS and Windows. `POST /convert` (fm-saxml), `/context`, `/debug`, `/lint`, `/pending` and `/preview` work on both. Endpoints that drive FileMaker through AppleScript (`/trigger`, `GET /clipboard`) are macOS-only. `POST /clipboard` works on macOS; on Windows, run `clipboard_win.py` directly for now. `POST /webviewer/stop` needs macOS or Linux.

---

## Starting the server

The easiest way is a launcher from `agent/scripts/launchers/`: `start_companion.cmd` (or `.ps1`) on Windows, `start_companion.sh` on macOS. They find the right virtual environment, start the server in the foreground or the background, and optionally start it at logon (Task Scheduler on Windows, launchd on macOS); their status, attach and stop options call the server's own commands below. See [Starting, stopping and checking the companion](../../README.md#starting-stopping-and-checking-the-companion) in the README.

The server itself is a single Python file that needs only the standard library (`companion_state.py` and `companion_control.py` next to it are stdlib too), except for `POST /convert`, which runs fm-saxml with the server's own Python, so that Python needs `fm-saxml-converter` installed. To run it directly:

```bash
# Default port 8765
python3 agent/scripts/companion_server.py

# Custom port
python3 agent/scripts/companion_server.py --port 9000

# Auto-shut down after 45 minutes (2700s) with no requests
python3 agent/scripts/companion_server.py --idle-timeout 2700

# From another terminal: who is running, follow its log, stop it, restart it here
python3 agent/scripts/companion_server.py status
python3 agent/scripts/companion_server.py attach
python3 agent/scripts/companion_server.py stop
python3 agent/scripts/companion_server.py restart
```

`companion_server.py [start|status|stop|restart|attach] [options]`; no command means `start`. See [Finding, watching and stopping the companion](#finding-watching-and-stopping-the-companion).

### Configuration — `agent/config/companion.json`

The host, port, and advertise address are resolved from a single optional file, `agent/config/companion.json` (gitignored — copy `companion.json.example` to create it).
This is the single source of truth so the server and every client agree on one address.
The file is optional: absent or malformed, the server falls back to built-in defaults and still boots.

```jsonc
{
  "companion": {
    "bind_host": "127.0.0.1",        // interface the server binds on
    "port": 8765,                    // server port (single source of truth)
    "advertise_host": "local.hub",   // host clients dial to reach it (may differ from bind_host)
    "idle_timeout_seconds": 0,       // 0 = never auto-shutdown
    "log_file": ""                   // optional: the log file's path, or "off"; see Log file below
  }
}
```

`bind_host` / `port` govern where the server **binds**; `advertise_host` + `port` are what clients (`deploy.py`, tests) **dial**.
A client can never change how the server binds.

Resolution precedence, highest wins:

- **Server bind** — CLI flag (`--port`) / env var (`COMPANION_BIND_HOST`, `COMPANION_PORT`) → `companion.json` → defaults (`127.0.0.1:8765`).
- **Client reach** — `COMPANION_URL` env → `companion.json` `advertise_host` + `port` → legacy `automation.json` `companion_url` (deprecation window) → default.
- **Log file** — `--no-log-file` → `--log-file` → `COMPANION_LOG_FILE` env → `companion.json` `log_file` → `companion-server.log` in the state folder. See [Log file](#log-file).

The plug-in's Application Support path is deliberately **not** configurable here — it is a fixed macOS platform location with one correct value, resolved against the macOS user running the companion (which must be the same user running FileMaker and the plug-in). A relocated or cross-user path is a deployment error to document, not a config knob.

### Idle auto-shutdown

By default the server runs until you stop it (`Ctrl-C`, `companion_server.py stop`, `launchctl unload`, etc.). Pass `--idle-timeout <seconds>` (or set the `COMPANION_IDLE_TIMEOUT` environment variable) to have it wind down on its own after a stretch with no requests. This is handy when the server is started per work session — a launchd job at login, or a Claude Code `SessionStart` hook — and you'd rather it not sit resident overnight.

A value of `0` (the default) disables auto-shutdown, so existing always-on setups are unaffected. Any incoming request resets the timer, so the server never stops mid-task.

Startup log output:

```
2026-03-09T14:22:01 INFO companion_server v1.0 listening on 127.0.0.1:8765 (PID 41872)
2026-03-09T14:22:01 INFO Log file: C:\Users\me\AppData\Local\agentic-fm\companion-server.log (debug lines; default)
2026-03-09T14:22:01 INFO Endpoints: GET /health  ...  GET /convert/<job id>  ...  POST /shutdown  POST /convert  POST /explode (deprecated alias of /convert)  ...
2026-03-09T14:22:01 INFO Press Ctrl-C to stop (or run `companion_server.py stop` from another terminal).
```

### Log file

The companion writes its own log file, however it was started (a terminal, an IDE task, a launcher, the Windows logon task or launchd), so you can read what it did afterwards and follow it from another terminal (`companion_server.py attach`).

- **Where:** `companion-server.log` in the per-user [state folder](#the-state-folder): `%LOCALAPPDATA%\agentic-fm\` on Windows, `~/Library/Logs/agentic-fm/` on macOS, `${XDG_STATE_HOME:-~/.local/state}/agentic-fm/` on Linux and WSL. The startup log names it.
- **Elsewhere, or none:** `--log-file PATH`, the `COMPANION_LOG_FILE` environment variable, or `log_file` in `companion.json`, in that order (as for the port). `--no-log-file` turns it off; so does an empty value or `off` in the environment variable or `companion.json`.
- **Rotation:** by size, 5 MB per file, keeping five old files (`companion-server.log.1` to `.5`). UTF-8.
- **Level:** the file gets debug lines too (stage progress, FileMaker's `GET /convert/<job id>` polls, `GET /health` requests, successful `POST /progress` calls, file starts while the live panel is on, the state file's path); the console stays at info. File lines look like `2026-10-30 11:45:29,123 INFO companion_server: ...`.
- **Can't open it:** the companion warns and logs to the console only. The startup line says which: `Log file: <path> (debug lines; default)` (or `from --log-file`, `from $COMPANION_LOG_FILE`, `from companion.json log_file`), or `Log file: off (<why>)`.
- **Live panel:** the file keeps receiving every line while the [live progress panel](#live-progress-panel) is drawn.

Two companions with the same log file (two ports, one state folder) write into one file; give one of them `--log-file` if you run both. On Windows, a program holding the file open (an editor) can delay a rotation; the companion then keeps writing to the current file and tries again a minute later.

The launchers' background starts no longer redirect the console into a log: the companion's own file has everything. Python errors before the companion opens its file (a missing module, a syntax error) go to `companion.err.log` in the same folder.

### Live progress panel

While FileMaker exports a solution ([`POST /progress`](#post-progress)) and fm-saxml converts it ([`POST /convert`](#post-convert)), the companion draws one panel at the bottom of its terminal:

```
┌─ agentic-fm progress ───────────────────────────────────────────────────────────┐
│ export   Invoices  file 3 of 12 Invoices.fmp12                                  │
│                    ━━━━━━━━━━━━━━━  25.0s elapsed · about 30.0s left            │
│                    finished: Contacts.fmp12 2.0s, Lines.fmp12 4.0s              │
│ convert  Sales     Analyzing: file 2 of 5 Invoices                              │
│                    ━━━━━━━━━━━━━━━  1m 15s elapsed · about 24.0s left in this … │
└─────────────────────────────────────────────────────────────────────────────────┘
```

It shows the step (export, then convert with its fm-saxml stage), the file N of M, the time of each finished file, the total elapsed time and an estimate of the time left (the average time of the files finished so far in that step or stage, times the files still to do; "estimating" until the first file is done). A stage that counts units (`write_pages`, `write_viewer`, `check`) shows them next to the file, e.g. `Writing HTML viewer: 5,300 of 21,193 pages, file 1 of 1 Synapse`, and estimates from the unit rate instead (the stage's time so far per unit done, times the units left; "estimating" until the first unit). Log lines print above it.

While the panel is on, the lines for a file starting (`convert ...: Reading 1/3 idiClients`, `export ...: Exporting 1/3 Synapse.fmp12`) are logged at debug, since the panel shows the file in progress: the log keeps one line per file, when it is done. Stage starts and ends and the `done` lines stay. With the panel off, the start lines stay in the log: they are the only sign of life during a long file. The panel is there only while an export or a conversion is in progress: when nothing runs, the terminal shows the log alone, and the end of each run is in the log (`Export done`, `convert ... succeeded`). An export with no event for 30 minutes is taken as abandoned and leaves the panel.

The panel needs [Rich](https://github.com/Textualize/rich), which the DIS harness `.venv` has through fm-saxml, and a terminal (the VS Code task terminal counts). Otherwise the companion prints the plain log lines and no control codes:

- stdout is not a terminal (redirected to a file, a launchd or background job), or `TERM=dumb`;
- Rich cannot be imported (the companion itself needs only the standard library);
- `--no-live`, or the `COMPANION_NO_LIVE` environment variable set to `1` (or anything but empty, `0`, `false`, `no`).

The startup log says which applies, for example `Live progress panel: off, stdout is not a terminal; plain log lines only.` The code is in `agent/scripts/companion_live.py`.

### Background process (Mac)

To run the server in the background without blocking the terminal, use the launcher (`bash agent/scripts/launchers/start_companion.sh --background`), or by hand:

```bash
python3 agent/scripts/companion_server.py --launched-by background --no-attach > /dev/null 2>&1 &
```

The companion writes its own [log file](#log-file), so there is no need to redirect its output into one. Watch it with `companion_server.py attach`, stop it with `companion_server.py stop`.

### Auto-start with launchd (Mac)

`bash agent/scripts/launchers/start_companion.sh --install-launchd` writes and loads a plist like the one below for you; `--uninstall-launchd` removes it. The agent runs the launcher with `--launchd`, so the venv is looked up at each start and the companion says `launched_by: launchd`; if a companion is already running then, the launcher leaves it alone and exits 0. The companion logs to its own [log file](#log-file); launchd's stderr file, `companion.err.log`, only gets Python's errors from before it opens it.

`KeepAlive` with `SuccessfulExit` false restarts the server only after a failed exit (a crash, or death by `SIGKILL`), not after a clean stop: `companion_server.py stop`, the idle timeout and `SIGTERM` (handled like Ctrl-C) all exit 0, so it stays stopped until your next login. A plist installed by an older launcher (stdout into `companion.log`, no `--launchd`) keeps working; install it again to get the new log handling and `launched_by`.

To write it by hand instead, replace the paths with your actual username and repo location:

**`~/Library/LaunchAgents/com.agentic-fm.companion-server.plist`**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.agentic-fm.companion-server</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/yourname/agentic-fm/.venv/bin/python</string>
        <string>/Users/yourname/agentic-fm/agent/scripts/companion_server.py</string>
        <string>--launched-by</string>
        <string>launchd</string>
        <string>--no-attach</string>
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/yourname/agentic-fm</string>

    <key>StandardOutPath</key>
    <string>/dev/null</string>

    <key>StandardErrorPath</key>
    <string>/Users/yourname/Library/Logs/agentic-fm/companion.err.log</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
</dict>
</plist>
```

Load it immediately without logging out:

```bash
launchctl load ~/Library/LaunchAgents/com.agentic-fm.companion-server.plist
```

Verify it is running:

```bash
launchctl list | grep agentic-fm
python3 agent/scripts/companion_server.py status
```

To unload (stop and disable auto-start):

```bash
launchctl unload ~/Library/LaunchAgents/com.agentic-fm.companion-server.plist
```

---

## Finding, watching and stopping the companion

A companion started in the background, by the logon task or launchd, or from an IDE run that was closed keeps running out of sight. The companion therefore says who it is, keeps a state file, and has commands to check, follow and stop it from any terminal, on Windows, macOS and Linux.

### The state folder

One per-user folder holds the [log file](#log-file), the state file and the [conversion records](#conversion-records):

| Platform | Folder |
|---|---|
| Windows | `%LOCALAPPDATA%\agentic-fm\` |
| macOS | `~/Library/Logs/agentic-fm/` |
| Linux, WSL | `${XDG_STATE_HOME:-~/.local/state}/agentic-fm/` |

The `COMPANION_STATE_DIR` environment variable moves it (mainly for tests; set it for the server and for every `status`/`stop`/`attach` call alike). On macOS and Linux the companion creates it readable only by you (mode 700); on Windows your profile folder's permissions already do that.

### The state file

Once its port is bound, the companion writes `companion.state.json` in the state folder: the same fields as [`GET /health`](#get-health) (`pid`, `started_at`, `host`, `port`, `afm_root`, `python`, `log_file`, `launched_by`) plus a random `token`. It is written atomically, readable only by you (mode 600 on POSIX), and removed when the companion exits cleanly (Ctrl-C, `stop`, `POST /shutdown`, the idle timeout). A companion only ever removes its own file, never a newer one's.

A file left behind by a companion that was killed is *stale*: its PID is gone, or the PID now belongs to another program (its command line doesn't run `companion_server.py`). `status`, `stop`, `attach` and a second start say so, and `stop` and a second start remove it. The PID check never uses `os.kill(pid, 0)` on Windows (it would kill the process): it uses `OpenProcess`/`GetExitCodeProcess` there, and reads command lines with `NtQueryInformationProcess` (PowerShell CIM as a fallback); on macOS and Linux it uses `os.kill(pid, 0)`, `/proc` and `ps`.

The token guards [`POST /shutdown`](#post-shutdown): only a process that can read your state file can stop the companion. `GET /health` never returns it.

### Commands

`companion_server.py` takes a command first; no command means `start`. Every command accepts the same options, and `status`, `stop`, `attach` and `restart` look for the companion on the port `start` would use (`--port`, `COMPANION_PORT`, `companion.json`), at `127.0.0.1` when `bind_host` is `0.0.0.0` (`::1` for `::`). The old options (`--port`, `--idle-timeout`, `--no-live`) work before or after the command.

| Command | What it does | Exit code |
|---|---|---|
| `start` | Runs the companion in this terminal. If one already holds the port, see [A second start](#a-second-start). | the server's |
| `status` | Prints who is running on the port, from `GET /health` and the state file: PID, start time and uptime, address, folder, log file, how it was launched, Python, version, running conversion. Says when the state file is stale or names another companion, and when the companion is alive but doesn't answer HTTP. | 0 running (answering or not), 1 not |
| `attach` | Prints the same details, then the last 40 lines of its log file, and follows the file (across rotation and truncation; it waits for a file that isn't there yet). Ctrl-C asks `[d]etach (the companion keeps running) or [s]top it?`; Enter, `d`, a second Ctrl-C or end of input detach. When the companion stops, `attach` ends too. | 0, or 1 when nothing runs or it writes no log file |
| `stop` | Sends `POST /shutdown` with the state file's token, waits up to `--timeout` seconds (default 15) for it to exit, then terminates the PID from `/health` or the state file, but only after checking it is still a companion (on macOS and Linux `SIGTERM`, which the companion handles like Ctrl-C (clean shutdown, state file removed, exit 0), then `SIGKILL` if it is still there; on Windows `TerminateProcess`, which can't be caught, so the state file stays behind until the next `status` or `stop` removes it as stale. Ctrl-Break in the companion's window is handled like Ctrl-C). A companion that doesn't answer HTTP is terminated the same way, and so is one whose state file token is missing (unless `/health` shows a conversion and `--force` isn't given). A stale state file is removed ("Removed the stale state file ..."). Prints each step. While a conversion runs it refuses and prints the job; `--force` stops it anyway (the conversion is recorded as failed, "stopped by /shutdown"). | 0 stopped or nothing running, 1 failed, 2 refused (conversion running) |
| `restart` | `stop` (with `--timeout`, `--force`), then `start` in this terminal. It starts only when `stop` exits 0 (stopped, or nothing was running); otherwise it exits with `stop`'s code. | 1 stop failed, 2 refused (conversion running), else the server's |

Options for `start`:

| Option | Meaning |
|---|---|
| `--log-file PATH`, `--no-log-file` | See [Log file](#log-file). |
| `--replace` | If a companion already runs on the port, stop it and start here instead. |
| `--no-attach` | If a companion already runs on the port, print its details and exit 1. |
| `--launched-by WHO` | Shown by `/health` and `status`: `terminal` (default), `background`, `logon task`, `launchd` or `ide`. Also `COMPANION_LAUNCHED_BY`. The launchers and the harness's IDE tasks set it. |

While it runs in a terminal, the companion sets the terminal's title to `agentic-fm companion :<port> (PID <pid>)` (`SetConsoleTitleW` on Windows, the OSC 0 escape elsewhere), so you can find its window or tab.

### A second start

When the port is taken at start, the companion asks `GET /health` on it and reads the state file:

- **A companion answers:** it prints its PID, start time, folder, log file and running conversion, then attaches to it by default, as `attach` does. `--replace` stops it as `stop` does (so it refuses with exit 2 while a conversion runs) and starts here instead; `--no-attach` prints the details and exits 1. Without a terminal (stdin or stdout not a terminal: a launcher's background start, launchd, a pipe) it never attaches: it prints the details and exits 1, unless `--replace` is given.
- **A companion holds the port but doesn't answer:** it says so with the state file's details; `--replace` terminates it and starts here, otherwise it exits 1.
- **Another program holds the port:** it names it, with its PID (`GetExtendedTcpTable` on Windows, `lsof` or `ss` elsewhere), and exits 1 (`--replace` never stops other programs). Stop that program, or start on another `--port`.
- **The state file and the port disagree:** a stale state file, a state file naming a companion on another port, or a companion started from another agentic-fm folder: it says so (and removes a stale file).

### Find, watch and stop it, per platform

From the agentic-fm folder, with the virtual environment's Python (the launchers find it for you):

| | Windows | macOS, Linux |
|---|---|---|
| Who is running | `agent\scripts\launchers\start_companion.cmd -Status` | `bash agent/scripts/launchers/start_companion.sh --status` |
| Follow its log | `start_companion.cmd -Attach` | `start_companion.sh --attach` |
| Stop it | `start_companion.cmd -Stop` (add `-- --force` during a conversion) | `start_companion.sh --stop` (add `-- --force`) |
| Directly | `.venv\Scripts\python.exe agent\scripts\companion_server.py status` (`attach`, `stop`) | `.venv/bin/python agent/scripts/companion_server.py status` (`attach`, `stop`) |
| Read the log | `Get-Content -Wait -Tail 40 $env:LOCALAPPDATA\agentic-fm\companion-server.log` | `tail -f ~/Library/Logs/agentic-fm/companion-server.log` |

In the DIS harness, the VS Code tasks and JetBrains run configurations *agentic-fm: companion status*, *attach* and *stop* do the same. While the launchd agent is installed, `stop` stops the companion until your next login; `--uninstall-launchd` stops it for good. The Windows logon task likewise starts it again at your next logon.

### Conversion records

When a conversion finishes (succeeded, failed, or ended by a crash or a shutdown), the companion writes to `jobs/` in the state folder:

- `<job id>.json`: the job as `GET /convert/<job id>` returns it (without `success`), plus `command` (the fm-saxml arguments, as a list; the last run when it retried without `--progress`) and `log_file` (the path of the `.log` next to it);
- `<job id>.log`: fm-saxml's whole stdout and stderr in the order they came, `--progress` JSON lines included, and the Python traceback when the runner crashed (the job itself keeps only the last 4000 characters in `log_tail`);
- `last_job.json`: a copy of the latest record.

It keeps the newest 50 records (`CONVERT_MAX_JOBS`, by modification time; `last_job.json` stays). A record that can't be written is a warning in the log and never changes the job. When the conversion has an output folder (a conversion that succeeded), the same record is also written there as `_companion_job.json`. A conversion ended by a shutdown is recorded as failed: "stopped by /shutdown" (`stop --force`), or "stopped: the companion shut down" (Ctrl-C, the idle timeout). So you can see what happened after the companion has stopped or restarted.

---

## Endpoints

Every route, with its request and response schemas, is in [`companion_openapi.yaml`](companion_openapi.yaml) (OpenAPI 3.1).

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness check, who this companion is, and plug-in status |
| POST | `/shutdown` | Stop the companion (needs the state file's token) |
| POST | `/convert` | Queue an fm-saxml conversion |
| GET | `/convert/<job id>` | A conversion's state and results |
| POST | `/progress` | Report the export step's progress (FileMaker's export script) |
| POST | `/explode` | Deprecated alias of `POST /convert` |
| POST | `/context` | Write `CONTEXT.json` |
| GET, POST | `/pending` | Read and clear, or set, the pending paste job |
| GET, POST | `/clipboard` | Read FileMaker objects from, or write XML to, the clipboard |
| POST | `/trigger` | Run a FileMaker script through AppleScript (macOS) |
| POST | `/debug` | Write runtime debug state |
| POST | `/lint` | Lint FileMaker code with FMLint |
| GET | `/webviewer/status` | Whether the webviewer dev server is running |
| POST | `/webviewer/start`, `/webviewer/stop` | Start or stop the webviewer dev server |
| POST | `/webviewer/push` | Publish agent output for the webviewer |
| GET, POST | `/preview/<layout name>` | Serve or store a layout HTML preview |
| GET, POST | `/plugin/<path>` | Pass-through to the agentic-fm plug-in's HTTP server |

Rules that apply to every route:

- Paths are matched exactly, query string included: `GET /health?x=1` is a `404`. Only `GET /convert/<job id>` ignores a query string, and `POST /shutdown` reads `force` and `wait` from it.
- An unknown path answers `404` with `{"error": "Not found"}`. Only `GET` and `POST` are handled; there is no CORS support.
- Request bodies are JSON with a `Content-Length` header (Insert from URL sends one). A body without it is read as empty.
- Every request except `GET /health` counts as activity for the [idle auto-shutdown](#idle-auto-shutdown).

### GET /health

A lightweight liveness check. FileMaker or the developer can poll this to confirm the server is up before starting a conversion. It does not count as activity, so monitors can poll it without keeping the server alive.

**Request:**

```
GET http://localhost:8765/health
```

No request body. No required headers.

**Response (200 OK):**

```json
{
  "status": "ok",
  "version": "1.0",
  "plugin": { "installed": false, "usable": false, "server": {}, "license": {}, "solutions": [] },
  "pid": 41872,
  "started_at": "2026-10-30T14:22:01Z",
  "uptime_seconds": 3605.2,
  "host": "127.0.0.1",
  "port": 8765,
  "afm_root": "C:\\Dev\\agentic-fm",
  "python": "C:\\Dev\\harness\\.venv\\Scripts\\python.exe",
  "log_file": "C:\\Users\\me\\AppData\\Local\\agentic-fm\\companion-server.log",
  "launched_by": "background",
  "conversion": null,
  "viewers": [{ "output_path": "C:\\Users\\me\\SaXML_docs\\Invoices", "url": "http://127.0.0.1:8766/index.html" }]
}
```

| Field | Meaning |
|---|---|
| `pid` | The companion's process ID. |
| `started_at`, `uptime_seconds` | When it started (UTC), and how long it has been running. |
| `host`, `port` | The address it is bound to. |
| `afm_root` | The agentic-fm folder it runs from. |
| `python` | The Python it runs under (`POST /convert` runs fm-saxml with it). |
| `log_file` | Its [log file](#log-file), or `null` when it writes none. |
| `launched_by` | `terminal`, `background`, `logon task`, `launchd` or `ide` (`--launched-by`). |
| `conversion` | The running conversion (else the oldest queued one), as `GET /convert/<job id>` returns it without `success`, or `null`. |
| `viewers` | The `webmcp` viewers it is serving (see [POST /convert](#post-convert)): each one's `output_path` and `url`. `[]` when none. |

The [state file](#the-state-file) has the same fields (except `status`, `version`, `plugin`, `uptime_seconds`, `conversion` and `viewers`) plus the token, which `/health` never returns. `version` comes from `version.txt`. `plugin` describes the agentic-fm FileMaker plug-in, cached for about 60 seconds: whether it is installed and `usable` (installed, reachable and licensed), its server address and token, license details, indexed solutions and, when the plug-in is reachable, its live endpoint catalog in `discover`. See [PLUGIN_INTEGRATION.md](PLUGIN_INTEGRATION.md).

---

### POST /shutdown

Stops the companion through the same path as Ctrl-C: the server stops serving, a running conversion is ended (with `force`), the webviewer is stopped, the state file is removed, and the process exits 0. `companion_server.py stop` is the usual way to call it.

**Request:**

```
POST http://localhost:8765/shutdown
X-Companion-Token: <token from companion.state.json>
Content-Type: application/json

{"force": false, "wait": false}
```

The `X-Companion-Token` header must hold the `token` from the [state file](#the-state-file), so a web page can't stop the companion with a blind request to localhost. The body is optional; `force` and `wait` can also be sent as a query string (`/shutdown?force=1`, `?wait=1`).

| Response | When |
|---|---|
| `202` `{"status": "stopping"}` | Nothing is converting, or `force` is set (the running conversion is stopped and recorded as failed, "stopped by /shutdown"). |
| `202` `{"status": "stopping after job", "job": {...}}` | A conversion is running and `wait` is set: the companion stops once no conversion is queued or running. |
| `403` `{"error": "..."}` | The token header is missing or wrong. |
| `409` `{"error": "conversion running", "job": {...}}` | A conversion is queued or running, and neither `force` nor `wait` is set. |

A missing or wrong token gets `403`, an invalid JSON body `400`. Once a shutdown is accepted, new `POST /convert` and `POST /explode` requests get `503` (`{"success": false, "error": "The companion is stopping ..."}`). At shutdown the webviewer dev server is stopped too (Windows: `taskkill /T`; macOS, Linux: `SIGTERM` to its process group).

---

### POST /convert

Converts a Save a Copy as XML export with [fm-saxml](https://github.com/Blue-Kachina/fm-doc-saxml) in the background. The request returns a job ID straight away, so FileMaker's **Insert from URL** never waits on the conversion; poll `GET /convert/<job id>` until `done` is `true`.

The conversion runs `fm-saxml build` with the same Python the companion runs under, so start the companion with the Python that has `fm-saxml-converter` installed (the DIS harness `.venv`). Only one conversion runs at a time; others wait as `queued`.

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `solution_name` | string | Yes | The solution's name. The docs are written to `<docs root>/<solution_name>`, which is where `solution_context.py` (`load_solution` / `find_docs`) looks. For a multi-file export, send one name for the whole batch. |
| `export_path` | string | Yes | The export folder FileMaker wrote (a multi-file export), or a single `.xml` file. On Windows and macOS the path is used as sent; under WSL, `/C:/...` paths are translated to `/mnt/c/...`. |
| `docs_root` | string | No | The folder that holds every converted solution. Without it, the companion uses its default docs root (see [Where the docs go](#where-the-docs-go)). |
| `html` | boolean | No | Also write the HTML viewer (`index.html`), fm-saxml's `--html`. Default `false`. |
| `webmcp` | boolean | No | Make the viewer agent-ready with WebMCP tools, fm-saxml's `--webmcp`. Turns on `html`. Default `false`. |

```json
{
  "solution_name": "Invoices",
  "export_path": "C:\\Users\\you\\Desktop\\Invoices_2026-10-26_1405",
  "docs_root": "C:\\Users\\you\\SaXML_docs",
  "webmcp": true
}
```

`html` and `webmcp` must be JSON booleans; anything else (`"true"`, `1`, `null`) is refused with `400`. In FileMaker, send them with `JSONSetElement ( $body ; "webmcp" ; True ; JSONBoolean )`. They are the only fm-saxml switches a request can set: the companion always runs `fm-saxml build <export_path> --docs-root <docs root> --solution <solution_name> --force --yes`, plus `--html` / `--webmcp`.

Browsers give the viewer its WebMCP tools only when it is served over `http://127.0.0.1`, not when `index.html` is opened from disk. So when a `webmcp` job succeeds, the companion serves its docs folder with fm-saxml's server, on port 8766 (or a free port when that one is taken), without opening a browser. The job's `viewer_url` has the link, and the log shows it in a banner set off by `====` lines; `status` lists it as `Viewer`. It serves until the companion stops; a later `webmcp` build of the same solution reuses the server (it sends `no-store`, so reloading shows the new docs). If the server can't start (an fm-saxml without `serve`, or no port it can bind), the job still succeeds with `viewer_url` `""` and the log says why; serve the docs yourself with `fm-saxml serve <output_path>` (the `.venv`'s `fm-saxml`). These switches need an fm-saxml that has them; an older one rejects the switch, and the job fails with fm-saxml's message in `log_tail`.

**Started (202):**

```json
{ "success": true, "job_id": "3f9c0a1b2c4d", "state": "queued", "status_url": "/convert/3f9c0a1b2c4d" }
```

**Refused:** `400` for a bad body, a missing field, an export path that doesn't exist, or no docs root; `409` (with the running `job_id`) when that solution is already converting. Both have `"success": false` and an `error` message.

#### Where the docs go

fm-saxml writes each conversion to `<docs root>/<solution name>` (the name made safe for a folder). The docs root is the first of:

1. `docs_root` in the request body;
2. the `FM_SAXML_DOCS_ROOT` environment variable where the companion runs;
3. the DIS harness's `exports/` folder, when agentic-fm is the harness's `tools/agentic-fm` submodule (its `.git` is a file, its parent folder is `tools`, and the harness has a `.gitmodules`). The folder is created if it is missing.

If none applies, `/convert` answers `400` with a message naming these three options. At startup the companion logs its default, for example `Docs root: D:\dis-harness\exports (harness exports/)`, or `Docs root: none set`. Every job reports the `docs_root` it used.

### GET /convert/&lt;job id&gt;

The job's state. `state` is `queued`, `running`, `succeeded` or `failed`; `done` is `true` once it is `succeeded` or `failed`. A failed or crashed conversion is reported here — it never stops the companion.

```json
{
  "success": true,
  "job_id": "3f9c0a1b2c4d",
  "state": "succeeded",
  "done": true,
  "solution_name": "Invoices",
  "export_path": "C:\\Users\\you\\Desktop\\Invoices_2026-10-26_1405",
  "docs_root": "C:\\Users\\you\\SaXML_docs",
  "html": true,
  "webmcp": true,
  "output_path": "C:\\Users\\you\\SaXML_docs\\Invoices",
  "created_at": "2026-10-26T14:05:12Z",
  "started_at": "2026-10-26T14:05:12Z",
  "finished_at": "2026-10-26T14:05:31Z",
  "exit_code": 0,
  "error": "",
  "progress": {
    "stage": null, "file": null, "index": 0, "total": 0, "elapsed": 18.6,
    "files": [
      { "stage": "read", "file": "Invoices_UI", "seconds": 0.4 },
      { "stage": "read", "file": "Invoices_Data", "seconds": 2.1 },
      { "stage": "analyze", "file": "Invoices_UI", "seconds": 0.9 }
    ]
  },
  "kind": "batch",
  "viewer_path": "C:\\Users\\you\\SaXML_docs\\Invoices\\index.html",
  "viewer_url": "http://127.0.0.1:8766/index.html",
  "counts": { "files": 2, "tables": 14, "fields": 220, "table_occurrences": 40, "relationships": 31,
              "layouts": 58, "scripts": 112, "custom_functions": 9, "value_lists": 12 },
  "files": [
    { "name": "Invoices_UI", "counts": { "tables": 1, "fields": 3, "...": 0 }, "warning_count": 2 },
    { "name": "Invoices_Data", "counts": { "tables": 13, "fields": 217, "...": 0 }, "warning_count": 0 }
  ],
  "warning_count": 2,
  "warnings": [
    { "file": "Invoices_UI", "code": "UNRESOLVED_REF", "entity": "script:Go Home", "message": "..." }
  ]
}
```

- `html` and `webmcp` are the switches the job ran with (`webmcp` turns on `html`).
- `progress` is where the build is, read from fm-saxml's `--progress json` output. It is `{}` until the first progress event, and stays `{}` with an fm-saxml that has no `--progress` (the companion then runs the build again without the option, once, and remembers that until it restarts). It is there while the job runs and after it finishes:
  - `stage`: the stage under way: `read`, `scrub`, `analyze`, `write_raw`, `write_pages`, then with `html`/`webmcp` `write_viewer` (Writing HTML viewer), then while scrubbing is on `check` (Checking redactions, the scrubbing guard scanning every written file). `null` after the last event, at the end of a successful build. `write_viewer` and `check` have one "file", the solution.
  - `file`: the FileMaker file being worked on, or `null` at the start and end of a stage.
  - `index` and `total`: that file's number in the stage (1-based) and the number of files in it. `index` is `0` at the start of a stage and equals `total` at its end.
  - `elapsed`: seconds since the build started.
  - `done`, `units`, `unit` and `stage_elapsed`, only while the stage counts units (`write_pages`, `write_viewer` and `check`, with an fm-saxml that sends `stage_progress`): `done` of `units` `unit` (`pages` or `files`) are finished, and `stage_elapsed` is the seconds since the stage started. fm-saxml updates them at most once a second; at the end of the stage `done` is the final count, which may be a little above or below `units` (the page count of `write_pages` is an estimate). The next stage's start, or the end of the build, removes them, so a stage without units has none.
  - `files`: one entry per finished file and stage, in order, with `seconds` for how long that file took in that stage.

  While a job runs, show it as for example `Analyzing 3/12 Invoices` with `JSONGetElement ( $response ; "progress.index" )` and its neighbours, or for a stage with units `Writing HTML viewer: 5300 of 21193 pages` from `progress.done` and `progress.units`. A time-left estimate for that stage is `stage_elapsed / done * ( units - done )`. The companion's log has one readable line per event (but not per `stage_progress`), such as `convert 3f9c0a1b2c4d: Analyzing 3/12 Invoices (4.2s)` or `convert 3f9c0a1b2c4d: Writing HTML viewer done: 21193 pages (108.3s)`.
- `counts`, `files`, `kind`, `viewer_path`, `viewer_url`, `warning_count` and `warnings` appear once the job has succeeded. `viewer_path` is the viewer's `index.html`, or `""` when the job was run without `html`. `viewer_url` is where the companion serves that viewer, or `""` when the job was run without `webmcp` or the server could not start. A job record read back after a restart keeps its old `viewer_url`, which no longer answers. Counts come from each file's `entities.json`; warnings come from each file's `Reports/warnings.md` (messages there are cut at 80 characters, and at most 200 warnings are listed — `warning_count` is always the full number).
- A failed job has `exit_code` (fm-saxml's exit code, or `null` if it never started), `error`, and `log_tail` — the last 4000 characters of fm-saxml's output.
- An unknown job ID gives `404`. The companion keeps the last 50 finished jobs; restarting it forgets them.

In FileMaker, read the fields with `JSONGetElement ( $response ; "state" )`, `JSONGetElement ( $response ; "counts.fields" )`, `JSONGetElement ( $response ; "warnings[0].message" )` and so on.

---

### POST /progress

FileMaker's export script calls this before and after each Save a Copy as XML, and once after the last file, so the companion can show the export step, which happens before `/convert` is called. It returns `200` at once: the companion only logs the event and keeps the latest export state of each solution in memory (for the [live progress panel](#live-progress-panel) and a later `/convert` of that solution). Like every request, it counts as activity for the idle auto-shutdown.

**Request body:**

| Field | Type | Required | Description |
|---|---|---|---|
| `solution_name` | string | Yes | The solution being exported, the same name later sent to `/convert`. Matched without case. |
| `stage` | string | Yes | The step; the export script sends `export`. |
| `event` | string | Yes | `file_start` before a file is saved, `file_done` after it, `done` once after the last file. |
| `file` | string | Yes | The file being exported, e.g. `Invoices.fmp12`. May be empty for `done`. |
| `index` | number | Yes | The file's number, 1 to `total`. Any whole number from 0 for `done`. |
| `total` | number | Yes | How many files the export has. |

```json
{"solution_name": "Invoices", "stage": "export", "event": "file_start",
 "file": "Invoices.fmp12", "index": 3, "total": 12}
```

`index` and `total` may be JSON numbers or integer text (`"3"`), since an Insert from URL body built as text often carries them that way.

**Response (200):**

```json
{ "success": true, "solution_name": "Invoices", "event": "file_start", "index": 3, "total": 12, "done": false }
```

**Refused (400):** invalid JSON, a body that is not an object, or a missing or wrong field (an empty name or stage, an unknown `event`, an empty `file` for a file event, `index` or `total` not a whole number of at least 0, or `index` outside 1 to `total` for a file event). The `error` message names each problem; nothing is recorded.

**Log lines:**

```
2026-10-29T14:05:12 INFO export Invoices: Exporting 3/12 Invoices.fmp12
2026-10-29T14:05:20 INFO export Invoices: Exported 3/12 Invoices.fmp12 (8.4s)
2026-10-29T14:06:53 INFO export Invoices: Export done: 12 file(s) in 101.2s
```

The `Exporting` line is logged at debug while the [live progress panel](#live-progress-panel) is on. The time on `file_done` is measured by the companion from the `file_start` with the same `index` and `file`; without one, the line says the time is unknown. A `file_start` with `index` 1, or any file event after a `done`, starts a new run of that solution and clears its earlier files. Restarting the companion forgets every run.

---

### POST /explode (deprecated)

**Deprecated — use `POST /convert`.** For one release, `/explode` is an alias of `/convert`: it takes the old body (`export_file_path` stands in for `export_path`; `repo_path` and `exploder_bin_path` are ignored), starts the same background job, and adds `"deprecated": true` and a `deprecation` message to the response. The companion also logs a warning each time it is called. `docs_root` can be added to the old body; otherwise the default docs root applies (see [Where the docs go](#where-the-docs-go)).

The rest of this section describes the old `fmparse.sh` behaviour, which no longer runs. `fmparse.sh` and the runner code are removed in a later release.

Accepts a JSON payload describing the export to parse, invokes `fmparse.sh` as a subprocess, and returns the exit code and output so FileMaker can detect success or failure.

**Request headers:**

```
Content-Type: application/json
Content-Length: <byte length of body>
```

**Request body — JSON schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `solution_name` | string | Yes | The solution identifier. Used by `fmparse.sh` as the subfolder name under `xml_exports/` and `agent/xml_parsed/`. Must match the name used when the XML was exported. |
| `export_file_path` | string | Yes | Absolute path to the FileMaker XML export file (or directory of XML exports) on the local machine. Tilde expansion is supported (`~/...`). |
| `repo_path` | string | Yes | Absolute path to the root of the agentic-fm repository. `fmparse.sh` is resolved at `{repo_path}/fmparse.sh`. Tilde expansion is supported. |
| `exploder_bin_path` | string | No | Absolute path to the `fm-xml-export-exploder` binary, if it is not on `PATH`. Passed through to `fmparse.sh` as the `FM_XML_EXPLODER_BIN` environment variable. |

**Example request body:**

```json
{
  "solution_name": "Invoice Solution",
  "export_file_path": "/Users/yourname/Desktop/InvoiceSolution.xml",
  "repo_path": "/Users/yourname/agentic-fm",
  "exploder_bin_path": "~/bin/fm-xml-export-exploder"
}
```

**Success response (200 OK):**

Returned when `fmparse.sh` exits with code `0`.

```json
{
  "success": true,
  "exit_code": 0,
  "stdout": "==> Parsing Invoice Solution\n==> Done.\n",
  "stderr": ""
}
```

**Failure response (500):**

Returned when `fmparse.sh` exits with a non-zero code (e.g. the exploder binary is missing or the export file cannot be read).

```json
{
  "success": false,
  "exit_code": 1,
  "stdout": "==> Parsing Invoice Solution\n",
  "stderr": "ERROR: fm-xml-export-exploder: command not found\n"
}
```

**Validation error response (400):**

Returned when required fields are missing or the request body is not valid JSON.

```json
{
  "success": false,
  "exit_code": -1,
  "error": "Missing required fields: solution_name, repo_path"
}
```

**What the server actually runs:**

The server constructs and executes this command as a subprocess, with `cwd` set to `repo_path`:

```bash
{repo_path}/fmparse.sh -s "{solution_name}" "{export_file_path}"
```

If `exploder_bin_path` is provided, it is injected into the subprocess environment as `FM_XML_EXPLODER_BIN` before the command runs. `fmparse.sh` reads this variable to locate the exploder binary without requiring it to be on `PATH`.

---

### POST /context

Writes a CONTEXT.json file to the agentic-fm project on the host. Called by the Push Context FM script in server mode.

**Request body:**
```json
{ "repo_path": "/absolute/path/to/agentic-fm", "context": "{...}" }
```
`context` may be a pre-serialised JSON string or a parsed object.

**Response:** `{ "success": true, "path": "/path/to/CONTEXT.json" }`

---

### GET /pending

Returns and clears the pending paste job set by the most recent `/trigger` or `POST /pending` call. Called by the `Agentic-fm Paste` FM script to retrieve the target script name and flags without relying on AppleScript parameter passing (which is unreliable in FM Pro 22).

**Response:**
```json
{ "target": "ScriptName", "auto_save": false, "select_all": true }
```

Returns `{}` if no pending job is set. The job is cleared on read (consumed once).

---

### POST /pending

Sets the pending paste job directly (without triggering a script), replacing any job already set. Used for testing or custom trigger flows.

**Request body:**
```json
{ "target": "ScriptName", "auto_save": true, "select_all": true }
```

All fields are optional: `target` defaults to `""`, `auto_save` to `false`, `select_all` to `true`.

**Response:** `{ "success": true }`

---

### GET /clipboard

Reads the FileMaker objects on the macOS clipboard with `clipboard.py read` and returns them as XML. macOS only.

**Response:** `{ "success": true, "xml": "<fmxmlsnippet ...>" }`. `404` when the clipboard holds no FileMaker objects; `500` when the read fails or takes over 10 seconds.

---

### POST /clipboard

Accepts fmxmlsnippet XML content and writes it to the clipboard as FileMaker objects: with `clipboard.py` on macOS. Under WSL it calls `clipboard_win.py`, which now needs native Windows, so the write fails there; native Windows is not routed yet. Used by `deploy.py` (Tier 1/2/3) so the agent container can load the clipboard without running `osascript` directly.

**Request body:**
```json
{ "xml": "<?xml version=\"1.0\"?>..." }
```

**Response:** `{ "success": true }`. `400` without `xml`; `500` when the clipboard script fails.

---

### POST /trigger

Triggers FM Pro on the host to run a named FileMaker script via AppleScript (`osascript`). Used by `deploy.py` for Tier 2/3 automated deployment.

**Request body:**
```json
{
  "fm_app_name": "FileMaker Pro — 22.0.4.406",
  "script": "Agentic-fm Paste",
  "parameter": "TargetScriptName"
}
```
`script` is required unless `raw_applescript` is set. `fm_app_name` defaults to `FileMaker Pro` and must match the exact AppleScript application name (versioned, with em dash). `parameter`, `target_file`, `auto_save` and `select_all` are optional.

The AppleScript template used:
```applescript
tell application "FileMaker Pro — 22.0.4.406"
    activate
    tell document 1
        do script "Agentic-fm Paste"
    end tell
end tell
```

With `target_file`, `tell document 1` becomes `tell (first document whose name contains "<target_file>")`, so the right file is addressed when several are open.

**Response:** `{ "success": bool, "stdout": str, "stderr": str }`. This is `200` even when `osascript` fails: check `success` and `stderr`. `400` when neither `script` nor `raw_applescript` is sent; `500` when `osascript` takes over 30 seconds or is missing (not macOS).

**Parameter passing note:** FM Pro 22 does not reliably receive script parameters via `given parameter:` in `do script`, so the server does not send one. When `parameter` is provided, the server stores it as the pending job's `target` (with `auto_save` and `select_all`) before firing AppleScript. The triggered FM script calls `GET /pending` via Insert from URL to retrieve them. The pending job is cleared on first read.

**`auto_save` field:** Pass `"auto_save": true` to instruct `Agentic-fm Paste` to save all scripts after paste (via `Perform AppleScript: tell application "System Events" to keystroke "s" using {command down}`). Defaults to `false`.

**`select_all` field:** Passed to `Agentic-fm Paste` through the pending job. Defaults to `true`.

**Requirements:**
- macOS only (`osascript` must be available on the host)
- FM Pro must be running with the target solution open
- The `fmextscriptaccess` extended privilege (**Allow Apple events and ActiveX to perform FileMaker operations**) must be enabled on the account's privilege set in Manage Security. Without it, `do script` returns a privilege violation error (`-10004`) at runtime.
- For `auto_save`: FileMaker Pro must have Accessibility access granted in System Preferences → Privacy & Security → Accessibility so System Events can send keystrokes.
- For `raw_applescript` override (Tier 3 script creation): include `"raw_applescript": "tell application..."` to execute arbitrary AppleScript instead of the default template.

---

### POST /debug

Accepts a JSON payload of runtime debug state and writes it to `agent/debug/output.json` at the repo root, replacing the previous one. Called by the Agentic-fm Debug FM script.

**Request body:** Any JSON object (typically `$$DEBUG` variable contents from FM).

**Response:** `{ "success": true, "path": "/path/to/output.json" }`

---

### POST /lint

Lints FileMaker code with the FMLint engine (`agent/fmlint`).

**Request body:**
```json
{ "content": "Set Variable [ $x ; Value: 1 ]", "format": "hr", "tier": 2, "disable": ["N003"] }
```

| Field | Type | Required | Description |
|---|---|---|---|
| `content` | string | Yes | fmxmlsnippet XML or a human-readable script. |
| `format` | string | No | `xml` or `hr`; detected from the content when omitted. |
| `tier` | integer | No | Highest rule tier to run (1–3). |
| `disable` | array | No | Rule IDs to skip. |

**Response:**
```json
{
  "source": "",
  "ok": false,
  "error_count": 1,
  "warning_count": 0,
  "diagnostics": [
    { "rule_id": "B001", "severity": "error", "message": "...", "line": 1, "column": 0,
      "end_line": 0, "end_column": 0, "fix_hint": "..." }
  ]
}
```

`severity` is `error`, `warning`, `info` or `hint`. `line` and `column` are 1-based; `0` means the whole file or the whole line. `fix_hint` appears only when FMLint has one. Errors are `{"error": "..."}` without `success`: `400` for a bad body or missing `content`, `500` when FMLint raises.

---

### Webviewer endpoints

These drive the agentic-fm webviewer (`webviewer/`) from the agent.

- **`GET /webviewer/status`** → `{ "running": bool }`: whether the dev server the companion started is still running.
- **`POST /webviewer/start`** with `{ "repo_path": "/path/to/agentic-fm" }` runs `npm run dev` in `<repo_path>/webviewer`. Response: `{ "success": true, "status": "started", "pid": 1234 }`, or `"status": "already_running"`. `400` without `repo_path`; `500` if npm cannot start.
- **`POST /webviewer/stop`** (no body) stops the dev server's process group. Response: `"status": "stopped"` or `"not_running"`. macOS and Linux only.
- **`POST /webviewer/push`** writes agent output to `<repo_path>/agent/config/.agent-output.json`, which the webviewer polls:

  ```json
  { "type": "diff", "repo_path": "/path/to/agentic-fm", "content": "...", "before": "...", "styles": "..." }
  ```

  `type` is `preview`, `diff`, `result`, `diagram` or `layout-preview`. `content` and `before` default to `""`; `styles` (CSS for `layout-preview`) is written only when sent. The file also gets a `timestamp`. Response: `{ "success": true, "path": "..." }`.

---

### GET /preview/&lt;layout name&gt;

Serves a layout's HTML preview as `text/html`: `agent/sandbox/<layout name>_web.html` if it exists, else the `content` of `agent/config/.agent-output.json`, else a "No preview available" heading. Always `200`. See [LAYOUT_PREVIEW_SYSTEM.md](../../docs/LAYOUT_PREVIEW_SYSTEM.md).

The layout name is used as-is in the file name; the server does not URL-decode it, so `/preview/My%20Layout` looks for `My%20Layout_web.html`.

### POST /preview/&lt;layout name&gt;

Stores a preview at `agent/sandbox/<layout name>_web.html`, replacing any earlier one.

**Request body:** `{ "html": "<!doctype html>...", "solution": "Invoices" }` (`solution` is accepted but not used).

**Response:** `{ "success": true, "path": "agent/sandbox/<layout name>_web.html" }`. `400` without `html`.

---

### GET and POST /plugin/&lt;path&gt;

An optional pass-through to the agentic-fm FileMaker plug-in's HTTP server, for clients that can reach the companion but not the plug-in's port (a container, for example) or that want one base URL. Direct plug-in access is the baseline.

The companion appends `<path>` (query string included) to the plug-in's base URL, adds its bearer token, forwards a POST body with its `Content-Type`, and returns the plug-in's status, `Content-Type` and body unchanged. For example, `GET /plugin/api/discover` returns the plug-in's endpoint catalog.

When the plug-in is not usable (not installed, unreachable or unlicensed) or the call fails, the companion answers `502` with `{"success": false, "error": "..."}`. See [PLUGIN_INTEGRATION.md](PLUGIN_INTEGRATION.md).

---

## Security

**This project is designed exclusively for local development.** It assumes you are working on your own machine, behind a firewall, on a private network. It is not hardened for production use, multi-user environments, or any network-accessible deployment. Do not use it on a public network or expose any part of it to the internet.

The server binds exclusively to `127.0.0.1` (localhost) by default. It is not reachable from other machines on the network — only processes running on the same machine can connect. No authentication is implemented, which is acceptable because the attack surface is limited to local processes already running under the same user account. The one exception is `POST /shutdown`, which needs the token from the user-only state file, so that a web page in a browser can't stop the companion with a blind request to localhost.

Do not set `bind_host` (`companion.json`) or `COMPANION_BIND_HOST` to `0.0.0.0`, or expose the server through a reverse proxy. Endpoints such as `/convert`, `/trigger` (which accepts arbitrary AppleScript) and `/webviewer/start` run programs, and `/context`, `/debug`, `/webviewer/push` and `/preview` write files, all with the permissions of the user who started the server.

> **Note for Docker users:** When running the agent in a container, `COMPANION_BIND_HOST=0.0.0.0` is required so the container can reach the host-side server. This is still safe as long as the host machine is on a private, firewalled network — the port should never be forwarded to a public interface.

---

## FileMaker integration

The export script sends one `POST /convert` with the export folder and one solution name for the whole (multi-file) batch, then polls `GET /convert/<job id>` every few seconds until `done` is `true`, and reads `state`, `counts` and `warnings` with `JSONGetElement`. See [POST /convert](#post-convert).

The older "Explode XML" companion script, described below, still works for one release through the deprecated `/explode` alias, but it now gets back a job ID instead of the finished result.

FileMaker calls the server from an "Explode XML" companion script using the **Insert from URL** step. The step is configured with:

- **URL:** `http://localhost:8765/explode`
- **Method:** POST
- **cURL options:** `--header "Content-Type: application/json" --data @$json_payload`

The FileMaker script builds the JSON payload by assembling field values and preference globals into a Let calculation, then fires the request. A typical payload as assembled in FileMaker:

```json
{
  "solution_name": "Invoice Solution",
  "export_file_path": "/Users/yourname/Desktop/InvoiceSolution.xml",
  "repo_path": "/Users/yourname/agentic-fm"
}
```

After Insert from URL completes, the script parses the response JSON, checks `success`, and branches accordingly — displaying an error dialog if `success` is `false` or proceeding to refresh context if the parse succeeded.

If Insert from URL itself fails (e.g. the server is not running), FileMaker displays its own connection-refused dialog before the script can evaluate the response.

---

## Troubleshooting

### Port already in use

When the port is taken at start, the companion finds out what holds it (see [A second start](#a-second-start)):

```
Port 8765 on 127.0.0.1 is already in use by a companion server:
  PID         41872
  Started     2026-10-30T14:22:01Z (up 1h 0m)
  Address     127.0.0.1:8765
  Folder      C:\Dev\agentic-fm
  Log file    C:\Users\me\AppData\Local\agentic-fm\companion-server.log
  Launched by background
```

That is your own companion, started earlier and forgotten. In a terminal the new start attaches to it and follows its log; Ctrl-C then asks whether to detach or stop it. `companion_server.py stop` stops it, and `--replace` stops it and starts the new one in its place.

```
Port 8765 on 127.0.0.1 is already in use by java.exe (PID 5120), not a companion server. Stop that program, or start this one with --port.
```

Another program holds the port: stop it, or run the companion on another port. When the PID can't be found, the message gives the `netstat -ano | findstr :8765` (Windows) or `lsof -i :8765` (macOS, Linux) command to find it.

On Windows the companion takes its port exclusively (`SO_EXCLUSIVEADDRUSE`, and no `SO_REUSEADDR`). Before, a second companion could listen on the same port without any error, and FileMaker's requests then reached either one: one terminal showed nothing while the other did the work. A companion from before this change can still share a port with another one from before it, so stop any old one that `netstat` shows.

Or start the server on a different port and update the URL in the FileMaker companion script:

```bash
python3 agent/scripts/companion_server.py --port 9000
```

### Server not running — FileMaker shows a connection dialog

If the server is not running when FileMaker executes Insert from URL, FileMaker displays a dialog: *"The URL could not be found."* or a similar network error. This is not a script logic failure — it means the companion server is not listening.

Start the server and retry, or check whether the launchd plist is loaded if auto-start is configured. `companion_server.py status` says whether a companion runs and on which port; a companion started with another `--port` than the one FileMaker calls doesn't count.

### fmparse.sh not found

The server constructs the `fmparse.sh` path as `{repo_path}/fmparse.sh`. If `repo_path` is wrong or the file is missing, `fmparse.sh` will fail to launch and the response will contain:

```json
{
  "success": false,
  "exit_code": -1,
  "error": "[Errno 2] No such file or directory: '/path/to/fmparse.sh'"
}
```

Verify that `repo_path` in the JSON payload matches the actual location of the agentic-fm repository root, and that `fmparse.sh` exists there:

```bash
ls /Users/yourname/agentic-fm/fmparse.sh
```

### Permission denied on fm-xml-export-exploder binary

`fmparse.sh` calls `fm-xml-export-exploder`. If the binary is not executable, the subprocess will fail with exit code 1 and `stderr` will contain a permission error. Fix it:

```bash
chmod +x ~/bin/fm-xml-export-exploder
```

If the binary is in a non-standard location and not on `PATH`, supply its full path in the `exploder_bin_path` field of the request payload.

### Diagnosing failures from the server log

Each request produces timestamped output, in the terminal (when there is one) and in the companion's [log file](#log-file), which also has the debug lines. `companion_server.py attach` follows the log of a companion running elsewhere; for a conversion that has finished, even before the companion was restarted, read its [record](#conversion-records) in `jobs/`.

```
2026-03-09T14:25:10 INFO 127.0.0.1 - "POST /explode HTTP/1.1" 200 -
2026-03-09T14:25:10 INFO Running fmparse.sh: solution='Invoice Solution' export='/Users/yourname/Desktop/InvoiceSolution.xml' cwd='/Users/yourname/agentic-fm'
2026-03-09T14:25:12 INFO fmparse.sh exited with code 0
```

The `stdout` and `stderr` fields in the response body contain the full output of `fmparse.sh`, which is the first place to look when the exit code is non-zero.

A `/convert` job logs the fm-saxml command, one line per progress event (`convert 3f9c0a1b2c4d: Analyzing 3/12 Invoices (4.2s)`; `stage_progress` updates are debug only, and so are file starts while the live panel is on) and fm-saxml's other output as it comes. FileMaker's `GET /convert/<job id>` polls and `GET /health` requests (which `attach` makes every few seconds) are logged at debug, so they show only in the log file; every other request is still logged in both.
