# Companion Server

The companion server is a lightweight HTTP server that exposes `fmparse.sh` as a local API endpoint. FileMaker calls it via the native **Insert from URL** script step, which functions as a curl-compatible HTTP client.

**Why this exists:** FileMaker has no built-in mechanism to execute arbitrary shell commands. The companion server fills this gap — no third-party plugin is required. As long as the server is running on the developer's machine, FileMaker can trigger XML parsing and context generation through a simple HTTP POST.

**Platforms:** The companion runs on macOS and Windows. `POST /convert` (fm-saxml) works on both. Endpoints that drive FileMaker through AppleScript (`/trigger`, the clipboard) are macOS-only, as is the deprecated `fmparse.sh` runner.

---

## Starting the server

The easiest way is a launcher from `agent/scripts/launchers/`: `start_companion.cmd` (or `.ps1`) on Windows, `start_companion.sh` on macOS. They find the right virtual environment, start the server in the foreground or the background, check `/health`, stop it, and optionally start it at logon (Task Scheduler on Windows, launchd on macOS). See [Starting, stopping and checking the companion](../../README.md#starting-stopping-and-checking-the-companion) in the README.

The server itself is a single Python file that needs only the standard library, except for `POST /convert`, which runs fm-saxml with the server's own Python, so that Python needs `fm-saxml-converter` installed. To run it directly:

```bash
# Default port 8765
python3 agent/scripts/companion_server.py

# Custom port
python3 agent/scripts/companion_server.py --port 9000

# Auto-shut down after 45 minutes (2700s) with no requests
python3 agent/scripts/companion_server.py --idle-timeout 2700
```

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
    "idle_timeout_seconds": 0        // 0 = never auto-shutdown
  }
}
```

`bind_host` / `port` govern where the server **binds**; `advertise_host` + `port` are what clients (`deploy.py`, tests) **dial**.
A client can never change how the server binds.

Resolution precedence, highest wins:

- **Server bind** — CLI flag (`--port`) / env var (`COMPANION_BIND_HOST`, `COMPANION_PORT`) → `companion.json` → defaults (`127.0.0.1:8765`).
- **Client reach** — `COMPANION_URL` env → `companion.json` `advertise_host` + `port` → legacy `automation.json` `companion_url` (deprecation window) → default.

The plug-in's Application Support path is deliberately **not** configurable here — it is a fixed macOS platform location with one correct value, resolved against the macOS user running the companion (which must be the same user running FileMaker and the plug-in). A relocated or cross-user path is a deployment error to document, not a config knob.

### Idle auto-shutdown

By default the server runs until you stop it (`Ctrl-C`, `launchctl unload`, etc.). Pass `--idle-timeout <seconds>` (or set the `COMPANION_IDLE_TIMEOUT` environment variable) to have it wind down on its own after a stretch with no requests. This is handy when the server is started per work session — a launchd job at login, or a Claude Code `SessionStart` hook — and you'd rather it not sit resident overnight.

A value of `0` (the default) disables auto-shutdown, so existing always-on setups are unaffected. Any incoming request resets the timer, so the server never stops mid-task.

Startup log output:

```
2026-03-09T14:22:01 INFO companion_server v1.0 listening on 127.0.0.1:8765
2026-03-09T14:22:01 INFO Endpoints: GET /health  ...  GET /convert/<job id>  ...  POST /convert  POST /explode (deprecated alias of /convert)  ...
2026-03-09T14:22:01 INFO Press Ctrl-C to stop.
```

### Background process (Mac)

To run the server in the background without blocking the terminal:

```bash
python3 agent/scripts/companion_server.py &
```

stdout logging will mix with your shell session. Redirect to a log file if that is disruptive:

```bash
python3 agent/scripts/companion_server.py > /tmp/companion_server.log 2>&1 &
```

### Auto-start with launchd (Mac)

`bash agent/scripts/launchers/start_companion.sh --install-launchd` writes and loads a plist like the one below for you (it runs the launcher, so the venv is looked up at each start, logs to `~/Library/Logs/agentic-fm/`, and restarts the server only if it crashes); `--uninstall-launchd` removes it. To write it by hand instead, replace the paths with your actual username and repo location:

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
    </array>

    <key>WorkingDirectory</key>
    <string>/Users/yourname/agentic-fm</string>

    <key>StandardOutPath</key>
    <string>/tmp/companion_server.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/companion_server.log</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>
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
curl http://localhost:8765/health
```

To unload (stop and disable auto-start):

```bash
launchctl unload ~/Library/LaunchAgents/com.agentic-fm.companion-server.plist
```

---

## Endpoints

### GET /health

A lightweight liveness check. FileMaker or the developer can poll this to confirm the server is up before starting a conversion.

**Request:**

```
GET http://localhost:8765/health
```

No request body. No required headers.

**Response (200 OK):**

```json
{
  "status": "ok",
  "version": "1.0"
}
```

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

```json
{
  "solution_name": "Invoices",
  "export_path": "C:\\Users\\you\\Desktop\\Invoices_2026-10-26_1405",
  "docs_root": "C:\\Users\\you\\SaXML_docs"
}
```

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
  "output_path": "C:\\Users\\you\\SaXML_docs\\Invoices",
  "created_at": "2026-10-26T14:05:12Z",
  "started_at": "2026-10-26T14:05:12Z",
  "finished_at": "2026-10-26T14:05:31Z",
  "exit_code": 0,
  "error": "",
  "kind": "batch",
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

- `counts`, `files`, `kind`, `warning_count` and `warnings` appear once the job has succeeded. Counts come from each file's `entities.json`; warnings come from each file's `Reports/warnings.md` (messages there are cut at 80 characters, and at most 200 warnings are listed — `warning_count` is always the full number).
- A failed job has `exit_code` (fm-saxml's exit code, or `null` if it never started), `error`, and `log_tail` — the last 4000 characters of fm-saxml's output.
- An unknown job ID gives `404`. The companion keeps the last 50 finished jobs; restarting it forgets them.

In FileMaker, read the fields with `JSONGetElement ( $response ; "state" )`, `JSONGetElement ( $response ; "counts.fields" )`, `JSONGetElement ( $response ; "warnings[0].message" )` and so on.

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

Returns and clears the pending paste job set by the most recent `/trigger` call. Called by the `Agentic-fm Paste` FM script to retrieve the target script name and `auto_save` flag without relying on AppleScript parameter passing (which is unreliable in FM Pro 22).

**Response:**
```json
{ "target": "ScriptName", "auto_save": false }
```

Returns `{ "target": "", "auto_save": false }` if no pending job is set. The job is cleared on read (consumed once).

---

### POST /pending

Sets the pending paste job directly (without triggering a script). Used for testing or custom trigger flows.

**Request body:**
```json
{ "target": "ScriptName", "auto_save": true }
```

**Response:** `{ "success": true }`

---

### POST /clipboard

Accepts fmxmlsnippet XML content and writes it to the macOS clipboard using `clipboard.py`. Used by `deploy.py` (Tier 1/2/3) so the agent container can load the clipboard without running `osascript` directly.

**Request body:**
```json
{ "xml": "<?xml version=\"1.0\"?>..." }
```

**Response:** `{ "success": true }`

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
`parameter` is optional. `fm_app_name` must match the exact AppleScript application name (versioned, with em dash).

The AppleScript template used:
```applescript
tell application "FileMaker Pro — 22.0.4.406"
    activate
    tell document 1
        do script "Agentic-fm Paste" given parameter:"TargetScriptName"
    end tell
end tell
```

**Response:** `{ "success": bool, "stdout": str, "stderr": str }`

**Parameter passing note:** FM Pro 22 does not reliably receive script parameters via `given parameter:` in `do script`. When `parameter` is provided, the server stores it in an internal pending job before firing AppleScript. The triggered FM script calls `GET /pending` via Insert from URL to retrieve the target and `auto_save` flag. The pending job is cleared on first read.

**`auto_save` field:** Pass `"auto_save": true` to instruct `Agentic-fm Paste` to save all scripts after paste (via `Perform AppleScript: tell application "System Events" to keystroke "s" using {command down}`). Defaults to `false`.

**Requirements:**
- macOS only (`osascript` must be available on the host)
- FM Pro must be running with the target solution open
- The `fmextscriptaccess` extended privilege (**Allow Apple events and ActiveX to perform FileMaker operations**) must be enabled on the account's privilege set in Manage Security. Without it, `do script` returns a privilege violation error (`-10004`) at runtime.
- For `auto_save`: FileMaker Pro must have Accessibility access granted in System Preferences → Privacy & Security → Accessibility so System Events can send keystrokes.
- For `raw_applescript` override (Tier 3 script creation): include `"raw_applescript": "tell application..."` to execute arbitrary AppleScript instead of the default template.

---

### POST /debug

Accepts a JSON payload of runtime debug state and writes it to `agent/debug/output.json` at the repo root. Called by the Agentic-fm Debug FM script.

**Request body:** Any JSON object (typically `$$DEBUG` variable contents from FM).

**Response:** `{ "success": true, "path": "/path/to/output.json" }`

---

## Security

**This project is designed exclusively for local development.** It assumes you are working on your own machine, behind a firewall, on a private network. It is not hardened for production use, multi-user environments, or any network-accessible deployment. Do not use it on a public network or expose any part of it to the internet.

The server binds exclusively to `127.0.0.1` (localhost) by default. It is not reachable from other machines on the network — only processes running on the same machine can connect. No authentication is implemented, which is acceptable because the attack surface is limited to local processes already running under the same user account.

Do not set `bind_host` (`companion.json`) or `COMPANION_BIND_HOST` to `0.0.0.0`, or expose the server through a reverse proxy. Endpoints such as `/convert` and `/trigger` run programs with the permissions of the user who started the server.

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

```
OSError: [Errno 48] Address already in use
```

Another process — possibly a previous instance of the companion server — is already bound to port 8765. Find and stop it:

```bash
lsof -i :8765
kill <PID>
```

Or start the server on a different port and update the URL in the FileMaker companion script:

```bash
python3 agent/scripts/companion_server.py --port 9000
```

### Server not running — FileMaker shows a connection dialog

If the server is not running when FileMaker executes Insert from URL, FileMaker displays a dialog: *"The URL could not be found."* or a similar network error. This is not a script logic failure — it means the companion server is not listening.

Start the server and retry, or check whether the launchd plist is loaded if auto-start is configured.

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

When the server is running in the foreground (or writing to a log file), each request produces timestamped output:

```
2026-03-09T14:25:10 INFO 127.0.0.1 - "POST /explode HTTP/1.1" 200 -
2026-03-09T14:25:10 INFO Running fmparse.sh: solution='Invoice Solution' export='/Users/yourname/Desktop/InvoiceSolution.xml' cwd='/Users/yourname/agentic-fm'
2026-03-09T14:25:12 INFO fmparse.sh exited with code 0
```

The `stdout` and `stderr` fields in the response body contain the full output of `fmparse.sh`, which is the first place to look when the exit code is non-zero.
