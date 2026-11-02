# Sandboxed & Virtualized Environment Guide

Instructions for AI agents running inside a sandboxed, containerized, or virtualized environment (Codex desktop app, Claude Code, Cursor, or similar). Read this document when you detect that you may not be running natively on the developer's macOS host.

---

## When to use this guide

This guide applies when **any** of the following are true:

- A user pastes a URL or says "set this up" / "install this" without further instructions
- You detect you are running in a sandboxed or virtualized environment (see detection below)
- The user mentions Codex, Claude Code sandbox, a container, or a VM
- You are running on Linux but the project clearly targets macOS + FileMaker Pro

If the developer is working interactively on macOS with FileMaker Pro open, use the standard `QUICKSTART.md` and `/setup` skill instead.

---

## Step 1: Detect your environment

Run these checks silently before presenting any setup plan. The results determine which setup path to follow.

```bash
# What OS?
uname -s          # "Darwin" = macOS, "Linux" = likely sandboxed

# Am I in a container?
test -f /.dockerenv && echo "DOCKER" || echo "NOT_DOCKER"
cat /proc/1/cgroup 2>/dev/null | grep -qi "docker\|containerd\|sandbox" && echo "CONTAINERIZED" || echo "NOT_CONTAINERIZED"

# Can I execute AppleScript? (macOS-only capability)
command -v osascript &>/dev/null && echo "OSASCRIPT_AVAILABLE" || echo "NO_OSASCRIPT"

# Can I reach the host?
curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/health 2>/dev/null || echo "NO_COMPANION_LOCAL"
curl -s -o /dev/null -w "%{http_code}" http://host.docker.internal:8765/health 2>/dev/null || echo "NO_COMPANION_DOCKER"

# Is the optional AgenticFM plug-in usable? (one call to the companion broker)
curl -s --max-time 5 http://localhost:8765/health 2>/dev/null | grep -q '"usable":true' && echo "PLUGIN_USABLE" || echo "PLUGIN_ABSENT_OR_OSS_ONLY"

# Is Rust/Cargo available? (needed to compile fm-xml-export-exploder on Linux)
command -v cargo &>/dev/null && echo "CARGO_AVAILABLE" || echo "NO_CARGO"

# Is fm-xml-export-exploder already installed?
command -v fm-xml-export-exploder &>/dev/null && echo "EXPLODER_FOUND" || echo "NO_EXPLODER"
test -x ~/bin/fm-xml-export-exploder && echo "EXPLODER_IN_BIN" || true

# Is the repo already populated with parsed XML?
test -d agent/xml_parsed && ls agent/xml_parsed/ 2>/dev/null | head -1 && echo "XML_PARSED_POPULATED" || echo "XML_PARSED_EMPTY"
```

Based on the results, classify your environment:

| Condition | Environment Type | Setup Path |
|-----------|-----------------|------------|
| `uname` = Darwin, `osascript` available | **Native macOS** | Use standard QUICKSTART.md |
| `uname` = Darwin, no `osascript` | **macOS sandbox** (e.g. Seatbelt) | Full-access path below |
| `uname` = Linux, host reachable | **Sandboxed with host access** | Full-access path below |
| `uname` = Linux, host not reachable | **Isolated sandbox** | Limited path below |

**Plug-in (optional enhancement):** if the `PLUGIN_USABLE` probe above succeeds, the commercial AgenticFM plug-in is installed, reachable, and licensed — prefer it per `agent/docs/PLUGIN_INTEGRATION.md`. On containerized or host-isolated agents, detection still comes through the companion's `/health` (it runs where it can see the plug-in), but **direct** plug-in access requires the plug-in's network-exposed mode (remote access on, `0.0.0.0` / `:8766`); otherwise route through the companion's `/plugin/<path>` proxy. The plug-in is never required — if the probe fails, continue on the pure OSS path.

---

## Step 2: Determine your access level

AI desktop apps (Codex, Claude Code, etc.) typically offer permission levels. The user may have granted:

- **Full access** — you can execute commands on the host, start background processes, install binaries, access the network. The project folder is shared between host and sandbox.
- **Limited/sandboxed** — you can read/write files in the project directory but cannot start persistent processes on the host or access the network freely.

**Ask the user if unclear.** A simple question like: "Do I have full system access, or am I restricted to this project folder?" saves significant trial and error.

---

## Full-Access Setup Path

When you have full access to the host (or are running natively), you can set up the entire project autonomously. This is the path for Codex "full access" mode or similar.

### 2a. Install fm-xml-export-exploder

This Rust binary parses FileMaker XML exports. It is required for the Explode XML workflow.

**On macOS (host or full-access sandbox):**

```bash
mkdir -p ~/bin

# Download the latest release binary for macOS
# Check https://github.com/bc-m/fm-xml-export-exploder/releases/latest for the current URL
# Bleeding-edge: https://github.com/petrowsky/fm-xml-export-exploder/releases
curl -L -o ~/bin/fm-xml-export-exploder "<release-url-for-macos>"
chmod +x ~/bin/fm-xml-export-exploder
```

On macOS, the first run may be blocked by Gatekeeper (unsigned binary). In a sandbox with full access, this restriction is typically bypassed. If you encounter it, run:

```bash
xattr -d com.apple.quarantine ~/bin/fm-xml-export-exploder
```

**On Linux (sandbox/container):**

No pre-built Linux binary is distributed. You have two options:

1. **Compile from source** (if Rust toolchain is available):
   ```bash
   # Install Rust if not present
   curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
   source "$HOME/.cargo/env"

   # Clone and build
   git clone https://github.com/bc-m/fm-xml-export-exploder.git /tmp/fm-xml-export-exploder
   cd /tmp/fm-xml-export-exploder
   cargo build --release
   mkdir -p ~/bin
   cp target/release/fm-xml-export-exploder ~/bin/
   chmod +x ~/bin/fm-xml-export-exploder
   cd -
   ```

2. **Skip and use pre-populated data** — if `agent/xml_parsed/` is already populated (e.g. the developer ran Explode XML on the host before handing the project to you), the binary is not needed for code generation. See the Limited Path below.

**Verify:**

```bash
~/bin/fm-xml-export-exploder --version
```

The `fmparse.sh` script finds the binary via `FM_XML_EXPLODER_BIN` environment variable or PATH lookup. Set it if the binary is not in `~/bin/` or PATH:

```bash
export FM_XML_EXPLODER_BIN="$HOME/bin/fm-xml-export-exploder"
```

### 2b. Verify Python 3 and xmllint

```bash
python3 --version       # Required — all core scripts use Python 3 stdlib only
xmllint --version       # Required by fmcontext.sh for index generation
```

If missing on Linux:

```bash
# Debian/Ubuntu
apt-get update && apt-get install -y python3 libxml2-utils

# Alpine
apk add python3 libxml2-utils
```

### 2c. Start the companion server

The companion server is a lightweight Python HTTP server (stdlib only, no dependencies) that bridges between FileMaker and the agent toolchain. Start it and keep it running in the background:

```bash
python3 agent/scripts/companion_server.py &
```

**Port:** 8765 (default). Override with `--port N`.

**Binding:** By default binds to `127.0.0.1`. If the sandbox needs to be reachable from a different network namespace (e.g. Docker), you can bind to all interfaces — but **only after verifying the host is on a private network and getting explicit user confirmation**.

> **⚠ Security warning — binding to 0.0.0.0**
>
> Binding to `0.0.0.0` exposes the companion server on **all network interfaces**, including any public-facing ones. An agent must **never** do this automatically. Before using `COMPANION_BIND_HOST=0.0.0.0`:
>
> 1. **Probe the current IP** and verify it falls within an RFC 1918 / RFC 4193 private range:
>    - `10.0.0.0/8`
>    - `172.16.0.0/12`
>    - `192.168.0.0/16`
>    ```bash
>    # Check all non-loopback IPv4 addresses
>    ip -4 addr show scope global 2>/dev/null | grep -oP 'inet \K[\d.]+' || \
>      ifconfig 2>/dev/null | grep -oP 'inet (\d+\.){3}\d+' | grep -oP '[\d.]+'
>    ```
>    If **any** interface has a public (non-private-range) IP, **do not bind to 0.0.0.0**.
>
> 2. **Ask the user for explicit confirmation**, e.g.:
>    > "The companion server needs to listen on all interfaces (`0.0.0.0`) so the container/VM can reach it. Your network interfaces are on private IPs (e.g. `192.168.1.x`). This is safe on a local network but **should not be used on public or untrusted networks**. Proceed?"
>
> 3. Only after both checks pass, start with:
>    ```bash
>    COMPANION_BIND_HOST=0.0.0.0 python3 agent/scripts/companion_server.py &
>    ```

**Verify it is running:**

```bash
curl -s http://localhost:8765/health
```

### 2d. Remaining FileMaker-side setup

The following steps require the developer to interact with FileMaker Pro directly. Present them clearly:

> **FileMaker setup required** — these steps must be done manually in FileMaker Pro:
>
> 1. **Install the Context custom function** — File > Manage > Custom Functions > New. Name: `Context`, parameter: `task` (Text). Paste contents of `filemaker/Context.fmfn`.
>
> 2. **Install companion scripts** — Open `filemaker/agentic-fm.fmp12`, copy the **agentic-fm** script folder, paste into your solution's Script Workspace.
>
> 3. **Set the repo path** — Run **Get agentic-fm path** from Scripts menu. Select this repo's root folder.
>
> 4. **Run Explode XML** — From Scripts menu, run **Explode XML**. This populates `agent/xml_parsed/`.
>
> 5. **Push Context** — Navigate to your target layout, run **Push Context**, enter a task description. This writes `agent/CONTEXT.json`.
>
> After these steps, the agent can generate FileMaker scripts autonomously.

### 2e. Verify the full setup

```bash
# Companion server responding
curl -s http://localhost:8765/health

# XML data populated
ls agent/xml_parsed/scripts_sanitized/ | head -5

# Context available
test -f agent/CONTEXT.json && python3 -c "import json; d=json.load(open('agent/CONTEXT.json')); print('Context:', d.get('task','(no task)'))"

# FMLint working
python3 -m agent.fmlint --help
```

---

## Limited (Filesystem-Only) Setup Path

When you are in a restricted sandbox with no host access and no network, you can still generate FileMaker code — but you depend on pre-populated data. The shared project folder is your communication channel.

### What works without host access

| Capability | Status | Notes |
|-----------|--------|-------|
| Read `agent/CONTEXT.json` | Works | If pre-populated by the developer on the host |
| Read `agent/xml_parsed/` | Works | If Explode XML was run on the host beforehand |
| Read index files (`agent/context/`) | Works | If fmcontext.sh was run on the host |
| Generate fmxmlsnippet XML | Works | Write to `agent/sandbox/` |
| Run FMLint validation | Works | `python3 -m agent.fmlint agent/sandbox/<file>` |
| Read step catalog | Works | `agent/catalogs/step-catalog-en.json` |
| Read coding conventions | Works | `agent/docs/CODING_CONVENTIONS.md` |
| Clipboard operations | Does not work | Requires macOS NSPasteboard or osascript |
| Deploy Tier 2/3 | Does not work | Requires AppleScript on macOS host |
| Run Explode XML | Does not work | Requires fm-xml-export-exploder + companion server |
| Start companion server usefully | Limited | No osascript = no `/trigger` or `/clipboard` endpoints |

### The filesystem bridge workflow

In this mode, the project folder mounted into your sandbox IS the communication channel between you and the host:

```
Developer (macOS host)                    Agent (sandbox)
─────────────────────                     ──────────────
1. Runs Explode XML in FileMaker
   → populates agent/xml_parsed/    ──→   Reads xml_parsed/

2. Runs Push Context in FileMaker
   → writes agent/CONTEXT.json      ──→   Reads CONTEXT.json

                                          3. Generates fmxmlsnippet XML
                                          → writes agent/sandbox/script.xml

4. Reads agent/sandbox/script.xml   ←──
   Pastes into FileMaker manually
```

### What to tell the developer

When you detect you are in a limited sandbox:

> **I'm running in a sandboxed environment without direct access to your macOS host.** I can generate FileMaker scripts, but I need you to handle the FileMaker-side operations:
>
> **Before I can help:**
> 1. Follow the setup in `QUICKSTART.md` on your Mac (install fm-xml-export-exploder, companion scripts, custom function)
> 2. Start the companion server: `python3 agent/scripts/companion_server.py`
> 3. Run **Explode XML** in FileMaker to populate `agent/xml_parsed/`
> 4. Run **Push Context** on the layout you're working on
>
> **After that, I can:**
> - Read your solution's structure, scripts, schema, and relationships
> - Generate new scripts and calculations as fmxmlsnippet XML
> - Validate output with FMLint
> - Write files to `agent/sandbox/` for you to paste into FileMaker
>
> **To paste my output into FileMaker**, run this on your Mac or Windows PC:
> ```bash
> python3 agent/scripts/clipboard.py write agent/sandbox/<filename>.xml
> ```
> Then switch to FileMaker's Script Workspace and press **Cmd+V** (Windows: **Ctrl+V**).

---

## Companion server and network topology

The companion server (`agent/scripts/companion_server.py`) is the HTTP bridge between FileMaker and the agent toolchain. Where it runs matters:

| Scenario | Where companion runs | How FM reaches it | How agent reaches it |
|----------|---------------------|-------------------|---------------------|
| **Native macOS** | Same machine as FM | `localhost:8765` | `localhost:8765` |
| **Agent in Docker on macOS** | macOS host | `localhost:8765` (FM) | `host.docker.internal:8765` |
| **FMS in Docker, agent on host** | macOS host | `host.docker.internal:8765` | `localhost:8765` |
| **Full-access sandbox** | Started by agent on host | `localhost:8765` | `localhost:8765` |
| **Restricted sandbox** | Developer starts on host | `localhost:8765` | Not reachable (use filesystem) |

**Key environment variable:** `COMPANION_BIND_HOST` controls the bind address. Default is `127.0.0.1`. Can be set to `0.0.0.0` when the companion needs to accept connections from containers or VMs — but **only on private networks (RFC 1918) and only with explicit user confirmation**. See the security warning in §2c above.

**Key config file:** `agent/config/automation.json` — the `companion_url` field can be set to `http://host.docker.internal:8765` for Docker scenarios.

---

## Platform-specific limitations

### Host-only components

These components need the host where FileMaker Pro runs (macOS, or Windows where noted) and cannot run on Linux:

| Component | macOS requirement | What it does |
|-----------|-------------------|-------------|
| `clipboard.py` | NSPasteboard (PyObjC) or osascript on macOS; the Win32 clipboard on native Windows | Read/write FileMaker clipboard format |
| `deploy.py` Tier 2/3 | osascript (AppleScript) on the companion's host | Automated paste into Script Workspace. With the companion on Windows, served as Tier 1 |
| `/trigger` AppleScript backend | osascript | Execute AppleScript commands remotely (its `fmp://` URL backend also works on Windows) |
| AX layout/script reading | Accessibility API (AXUIElement) | Read Script Workspace and layout objects |

### Cross-platform components

These work on any OS with Python 3:

| Component | What it does |
|-----------|-------------|
| `companion_server.py` (core) | HTTP server, `/explode`, `/context`, `/lint`, `/health` |
| `fmparse.sh` | Shell script — cross-platform if fm-xml-export-exploder binary exists |
| `fmcontext.sh` | Index generation from xml_parsed (requires xmllint) |
| `deploy.py` Tier 1 | File-based output only (no clipboard) |
| `python3 -m agent.fmlint` | XML and HR script validation |
| All code generation | Reading catalogs, CONTEXT.json, writing fmxmlsnippet XML |

---

## Quick-start script for autonomous agents

If you are an agent tasked with setting up this project and you have full access, run through this checklist in order:

```
1. Detect environment (Step 1 above)
2. Check Python 3 exists → install if missing
3. Check xmllint exists → install if missing
4. Check fm-xml-export-exploder exists → install if missing (compile from source on Linux)
5. Start companion server in background
6. Check if agent/xml_parsed/ is populated
   → If yes: ready to generate code
   → If no: tell developer to run Explode XML in FileMaker
7. Check if agent/CONTEXT.json exists
   → If yes: read task description, begin work
   → If no: tell developer to run Push Context in FileMaker
8. Read agent/docs/CODING_CONVENTIONS.md before generating any code
9. Scan agent/docs/knowledge/MANIFEST.md for task-relevant knowledge docs
10. Generate code → validate with FMLint → write to agent/sandbox/
```

For Tier 1 deployment (all platforms), present paste instructions:

> The script is ready at `agent/sandbox/<filename>.xml`. To install it:
>
> On the host (Mac or Windows PC), run:
> ```bash
> python3 agent/scripts/clipboard.py write agent/sandbox/<filename>.xml
> ```
>
> Then in FileMaker:
> 1. Open the target script in Script Workspace
> 2. **Cmd+A** (Windows: **Ctrl+A**) — select all existing steps
> 3. **Cmd+V** (Windows: **Ctrl+V**) — paste

---

## Exposing services from the sandbox

If your sandbox environment supports exposing ports (e.g. Docker with `-p`, or a desktop app with port forwarding settings), and the developer wants the FileMaker plugin to communicate directly with services you run:

1. **Companion server**: Bind to all interfaces with `COMPANION_BIND_HOST=0.0.0.0` — **only after verifying all host IPs are in RFC 1918 private ranges and getting explicit user confirmation** (see §2c security warning)
2. **Expose port 8765** through whatever mechanism the sandbox provides
3. **Update FileMaker scripts**: The developer may need to change companion URLs in their FM scripts from `localhost:8765` to the exposed address (or configure `automation.json`)
4. **Webviewer dev server**: If using the webviewer, expose port 8080 similarly

Consult your sandbox platform's documentation for port exposure. For example:
- Docker: `-p 8765:8765` flag
- Codex desktop: Check the access/permissions panel for port forwarding options
- Claude Code: Network access must be enabled; `host.docker.internal` may work for outbound

---

## Summary decision tree

```
User says "set this up"
  │
  ├─ Am I on macOS with full access?
  │   └─ YES → Follow QUICKSTART.md steps autonomously
  │            Install binary, start companion, guide FM setup
  │
  ├─ Am I on Linux with full host access?
  │   └─ YES → Full-access path above
  │            Compile exploder from source (or download if macOS host)
  │            Start companion, guide FM setup
  │
  ├─ Am I on Linux with network to host?
  │   └─ YES → Start companion locally (limited — no osascript)
  │            Use host.docker.internal:8765 if host companion exists
  │            Generate code via filesystem
  │
  └─ Am I completely isolated?
      └─ YES → Filesystem-only bridge
               Tell developer what to run on their Mac
               Generate code, write to agent/sandbox/
               Provide clipboard.py paste command
```
