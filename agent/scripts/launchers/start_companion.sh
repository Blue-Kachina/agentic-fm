#!/bin/bash
# Start, stop or check the agentic-fm companion server on macOS (and Linux).
#
# Runs agent/scripts/companion_server.py with a virtual environment's Python, never a
# python3 from PATH. The Python is, in order:
#   1. $AGENTIC_FM_PYTHON, if set
#   2. the DIS harness .venv (../../.venv from the agentic-fm root), when agentic-fm is
#      the harness's tools/agentic-fm submodule
#   3. .venv in the agentic-fm root
#   4. agent/.venv in the agentic-fm root
# That Python needs fm-saxml-converter installed for POST /convert to work.
#
# Usage:
#   start_companion.sh                      run in this terminal (Ctrl-C stops it)
#   start_companion.sh --background         run in the background; log in ~/Library/Logs/agentic-fm/
#   start_companion.sh --status             GET /health
#   start_companion.sh --stop               stop a running companion
#   start_companion.sh --install-launchd    start it at every login (launchd agent)
#   start_companion.sh --uninstall-launchd  undo --install-launchd
#   start_companion.sh --print-python       print the Python it would use
#   start_companion.sh -- --port 9000       anything after -- goes to companion_server.py

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AFM_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SERVER="$AFM_ROOT/agent/scripts/companion_server.py"
LABEL="com.agentic-fm.companion-server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
if [ "$(uname -s)" = "Darwin" ]; then
    LOG_DIR="$HOME/Library/Logs/agentic-fm"
else
    LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agentic-fm"
fi
LOG_FILE="$LOG_DIR/companion.log"

die() { echo "$*" >&2; exit 1; }

find_python() {
    local candidates=()
    if [ -n "${AGENTIC_FM_PYTHON:-}" ]; then
        [ -x "$AGENTIC_FM_PYTHON" ] || die "AGENTIC_FM_PYTHON is set to '$AGENTIC_FM_PYTHON', which is not an executable file."
        candidates+=("$AGENTIC_FM_PYTHON")
    fi
    # A submodule has a .git *file*; only then is ../../.venv the DIS harness's venv.
    if [ -f "$AFM_ROOT/.git" ]; then
        candidates+=("$AFM_ROOT/../../.venv/bin/python")
    fi
    candidates+=("$AFM_ROOT/.venv/bin/python" "$AFM_ROOT/agent/.venv/bin/python")
    local c
    for c in "${candidates[@]}"; do
        if [ -x "$c" ]; then
            (cd "$(dirname "$c")" && echo "$(pwd)/$(basename "$c")")
            return 0
        fi
    done
    {
        echo "No Python virtual environment found for the companion server. Looked for:"
        for c in "${candidates[@]}"; do echo "  $c"; done
        echo "Create one (python3 -m venv .venv && .venv/bin/python -m pip install fm-saxml-converter),"
        echo "or set AGENTIC_FM_PYTHON to a python executable."
    } >&2
    exit 1
}

companion_port() {
    if [ -n "${COMPANION_PORT:-}" ]; then echo "$COMPANION_PORT"; return; fi
    local config="$AFM_ROOT/agent/config/companion.json"
    if [ -f "$config" ]; then
        "$1" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("companion",{}).get("port",8765))' \
            "$config" 2>/dev/null && return
    fi
    echo 8765
}

running_pids() {
    pgrep -f "agent/scripts/companion_server.py" || true
}

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

action="run"
companion_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --background) action="background" ;;
        --status) action="status" ;;
        --stop) action="stop" ;;
        --install-launchd) action="install" ;;
        --uninstall-launchd) action="uninstall" ;;
        --print-python) action="print" ;;
        -h|--help) sed -n '2,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --) shift; companion_args+=("$@"); break ;;
        *) companion_args+=("$1") ;;
    esac
    shift
done

case "$action" in
    stop)
        pids="$(running_pids)"
        if [ -z "$pids" ]; then echo "No companion server is running."; exit 0; fi
        if [ -f "$PLIST" ]; then
            echo "Note: the launchd agent restarts the companion if it crashes; use --uninstall-launchd to stop it for good."
        fi
        for pid in $pids; do kill "$pid" && echo "Stopped companion server (PID $pid)."; done
        exit 0 ;;
    uninstall)
        if [ -f "$PLIST" ]; then
            launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
            rm -f "$PLIST"
            echo "Removed the launchd agent ($PLIST) and stopped it."
        else
            echo "There is no launchd agent at $PLIST."
        fi
        exit 0 ;;
esac

PYTHON="$(find_python)"

case "$action" in
    print)
        echo "$PYTHON"
        exit 0 ;;
    status)
        url="http://127.0.0.1:$(companion_port "$PYTHON")/health"
        if body="$(curl -s --max-time 5 "$url")" && [ -n "$body" ]; then
            echo "Companion is running at $url: $body"
            exit 0
        fi
        echo "Companion is not responding at $url."
        exit 1 ;;
esac

if ! "$PYTHON" -c 'import fm_saxml' 2>/dev/null; then
    echo "Warning: fm-saxml is not installed for $PYTHON; POST /convert jobs will fail until it is (pip install fm-saxml-converter)." >&2
fi

export PYTHONUTF8=1
cd "$AFM_ROOT"

case "$action" in
    install)
        [ "$(uname -s)" = "Darwin" ] || die "--install-launchd is for macOS."
        mkdir -p "$(dirname "$PLIST")" "$LOG_DIR"
        cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$(xml_escape "$SCRIPT_DIR/start_companion.sh")</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml_escape "$AFM_ROOT")</string>
    <key>StandardOutPath</key>
    <string>$(xml_escape "$LOG_FILE")</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$LOG_FILE")</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
</dict>
</plist>
EOF
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"
        echo "Installed and started the launchd agent ($PLIST). Log: $LOG_FILE"
        echo "Remove it with --uninstall-launchd."
        exit 0 ;;
    background)
        if [ -n "$(running_pids)" ]; then
            echo "A companion server is already running (see --status, --stop)."
            exit 0
        fi
        mkdir -p "$LOG_DIR"
        nohup "$PYTHON" "$SERVER" ${companion_args[@]+"${companion_args[@]}"} >> "$LOG_FILE" 2>&1 &
        echo "Started the companion server (PID $!) with $PYTHON"
        echo "Log: $LOG_FILE"
        exit 0 ;;
esac

echo "Starting the companion server with $PYTHON (Ctrl-C stops it)"
exec "$PYTHON" "$SERVER" ${companion_args[@]+"${companion_args[@]}"}
