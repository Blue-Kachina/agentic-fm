#!/bin/bash
# Start, stop, watch or check the agentic-fm companion server on macOS (and Linux).
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
# --status, --attach and --stop call the companion's own status, attach and stop commands:
# they find the running companion from its state file and GET /health, and --stop never
# kills a conversion halfway unless you add -- --force. The companion writes its own log,
# companion-server.log in ~/Library/Logs/agentic-fm/ (Linux: ~/.local/state/agentic-fm/).
#
# Usage:
#   start_companion.sh                      run in this terminal (Ctrl-C stops it)
#   start_companion.sh --background         run in the background (it still writes its own log)
#   start_companion.sh --status             who is running: PID, start time, folder, log file
#   start_companion.sh --attach             show and follow its log; Ctrl-C detaches or stops it
#   start_companion.sh --stop               stop it cleanly
#   start_companion.sh --stop -- --force    stop it even while a conversion is running
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
# The companion's state folder (log file, state file, conversion records); COMPANION_STATE_DIR moves it.
if [ -n "${COMPANION_STATE_DIR:-}" ]; then
    STATE_DIR="$COMPANION_STATE_DIR"
elif [ "$(uname -s)" = "Darwin" ]; then
    STATE_DIR="$HOME/Library/Logs/agentic-fm"
else
    STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/agentic-fm"
fi
LOG_FILE="$STATE_DIR/companion-server.log"
# Only Python's own errors before the companion opens its log file land here.
ERR_LOG_FILE="$STATE_DIR/companion.err.log"

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

xml_escape() {
    printf '%s' "$1" | sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g'
}

action="run"
companion_args=()
while [ $# -gt 0 ]; do
    case "$1" in
        --background) action="background" ;;
        --status) action="status" ;;
        --attach) action="attach" ;;
        --stop) action="stop" ;;
        --install-launchd) action="install" ;;
        --uninstall-launchd) action="uninstall" ;;
        --launchd) action="launchd" ;;     # what the launchd agent runs
        --print-python) action="print" ;;
        -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        --) shift; companion_args+=("$@"); break ;;
        *) companion_args+=("$1") ;;
    esac
    shift
done

if [ "$action" = "uninstall" ]; then
    if [ -f "$PLIST" ]; then
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
        rm -f "$PLIST"
        echo "Removed the launchd agent ($PLIST) and stopped it."
    else
        echo "There is no launchd agent at $PLIST."
    fi
    exit 0
fi

PYTHON="$(find_python)"
export PYTHONUTF8=1

# Exit status of the companion's own status command (0: one is running), its output kept quiet.
companion_running() {
    (cd "$AFM_ROOT" && "$PYTHON" "$SERVER" status ${companion_args[@]+"${companion_args[@]}"} >/dev/null 2>&1)
}

case "$action" in
    print)
        echo "$PYTHON"
        exit 0 ;;
    status|attach|stop)
        if [ "$action" = "stop" ] && [ -f "$PLIST" ]; then
            echo "Note: the launchd agent starts the companion again at your next login; --uninstall-launchd removes it."
        fi
        cd "$AFM_ROOT"
        exec "$PYTHON" "$SERVER" "$action" ${companion_args[@]+"${companion_args[@]}"} ;;
esac

if ! "$PYTHON" -c 'import fm_saxml' 2>/dev/null; then
    echo "Warning: fm-saxml is not installed for $PYTHON; POST /convert jobs will fail until it is (pip install fm-saxml-converter)." >&2
fi

cd "$AFM_ROOT"

case "$action" in
    install)
        [ "$(uname -s)" = "Darwin" ] || die "--install-launchd is for macOS."
        mkdir -p "$(dirname "$PLIST")" "$STATE_DIR"
        # KeepAlive restarts the companion only after a failed exit: a clean stop (exit 0) stays stopped.
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
        <string>--launchd</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$(xml_escape "$AFM_ROOT")</string>
    <key>StandardOutPath</key>
    <string>/dev/null</string>
    <key>StandardErrorPath</key>
    <string>$(xml_escape "$ERR_LOG_FILE")</string>
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
    launchd)
        # A companion already running (started by hand): exit 0, so KeepAlive doesn't retry every 10 s.
        if companion_running; then
            echo "A companion server is already running; the launchd agent leaves it alone."
            exit 0
        fi
        exec "$PYTHON" "$SERVER" --launched-by launchd --no-attach ${companion_args[@]+"${companion_args[@]}"} ;;
    background)
        if companion_running; then
            "$PYTHON" "$SERVER" status ${companion_args[@]+"${companion_args[@]}"} || true
            echo "A companion server is already running (see --status, --attach, --stop)."
            exit 0
        fi
        mkdir -p "$STATE_DIR"
        nohup "$PYTHON" "$SERVER" --launched-by background --no-attach ${companion_args[@]+"${companion_args[@]}"} \
            >/dev/null 2>>"$ERR_LOG_FILE" &
        echo "Started the companion server (PID $!) with $PYTHON"
        echo "Log: $LOG_FILE (watch it with --attach, stop it with --stop)"
        exit 0 ;;
esac

echo "Starting the companion server with $PYTHON (Ctrl-C stops it)"
exec "$PYTHON" "$SERVER" ${companion_args[@]+"${companion_args[@]}"}
