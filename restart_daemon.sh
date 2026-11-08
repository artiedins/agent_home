#!/usr/bin/env bash

# restart_daemon.sh - restart the home daemon the way a reboot would.
#
# systemd owns the daemon now (home-daemon.service, enabled at boot). This
# script stops that unit, hunts down any daemon still running outside systemd
# (the old `nohup ./home_daemon.py &` habit), then starts the unit again. Two
# daemons cannot coexist: they fight over the weather station port 8001 and
# over the single Telegram bot connection.
#
# Nothing running is a normal case: the script then just starts the daemon.
#
# The unit systemd reads is /etc/systemd/system/home-daemon.service. The copy in
# systemd/ is only for review and version control; this script reads it to warn
# about drift, and deleting it breaks nothing. See systemd/README.md.
#
# Watch it afterwards with:
#   tail -f daemon.log agent.log

set -uo pipefail

SERVICE=home-daemon
PROJECT_DIR=/home/artie/Projects/home
UNIT_FILE="/etc/systemd/system/${SERVICE}.service"
REPO_UNIT="$PROJECT_DIR/systemd/${SERVICE}.service"
DAEMON_LOG="$PROJECT_DIR/daemon.log"
DAEMON_SCRIPT="home_daemon.py"
WS_PORT=8001
TERM_WAIT=20
START_WAIT=20
PORT_WAIT=15

if [ "$(id -u)" -eq 0 ]; then
    SUDO=()
elif sudo -n true 2>/dev/null; then
    SUDO=(sudo -n)
else
    SUDO=(sudo)
fi

# PIDs of live home_daemon.py interpreters, from systemd or from a stray shell.
# The check is on /proc/<pid>/exe, not just the command line: a shell or editor
# whose arguments mention the script name would otherwise match.
daemon_pids() {
    local pid exe
    for pid in $(pgrep -f "$DAEMON_SCRIPT" 2>/dev/null); do
        [ "$pid" = "$$" ] && continue
        exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null) || continue
        case "${exe##*/}" in
            python*) echo "$pid" ;;
        esac
    done
}

stop_unit() {
    if "${SUDO[@]}" systemctl cat "$SERVICE.service" >/dev/null 2>&1; then
        echo "==> stopping ${SERVICE}.service"
        "${SUDO[@]}" systemctl stop "$SERVICE.service"
    else
        echo "==> ${SERVICE}.service is not installed, nothing for systemd to stop"
    fi
}

kill_strays() {
    local pids leftover i
    pids=$(daemon_pids)
    if [ -z "$pids" ]; then
        echo "==> no daemon left outside systemd"
        return 0
    fi
    echo "==> stopping daemon started outside systemd: $(echo "$pids" | tr '\n' ' ')"
    # shellcheck disable=SC2086
    kill -TERM $pids 2>/dev/null
    for i in $(seq "$TERM_WAIT"); do
        [ -z "$(daemon_pids)" ] && break
        sleep 1
    done
    leftover=$(daemon_pids)
    if [ -n "$leftover" ]; then
        echo "==> still alive after ${TERM_WAIT}s, sending SIGKILL: $(echo "$leftover" | tr '\n' ' ')"
        # shellcheck disable=SC2086
        kill -KILL $leftover 2>/dev/null
        sleep 1
    fi
    leftover=$(daemon_pids)
    if [ -n "$leftover" ]; then
        echo "error: could not kill $(echo "$leftover" | tr '\n' ' ')" >&2
        return 1
    fi
    return 0
}

wait_for_port() {
    local i
    for i in $(seq "$PORT_WAIT"); do
        ss -ltn 2>/dev/null | grep -q ":${WS_PORT}\b" && return 0
        sleep 1
    done
    return 1
}

start_unit() {
    local i
    if ! "${SUDO[@]}" systemctl cat "$SERVICE.service" >/dev/null 2>&1; then
        echo "error: ${UNIT_FILE} is missing." >&2
        # The repo copy is optional; never print a cp of a file that is not there.
        if [ -f "$REPO_UNIT" ]; then
            echo "  install it with: sudo cp $REPO_UNIT $UNIT_FILE" >&2
        else
            echo "  write ${UNIT_FILE} (see NOTES.md, 'Daemon under systemd'), then:" >&2
        fi
        echo "  sudo systemctl daemon-reload && sudo systemctl enable --now $SERVICE.service" >&2
        return 1
    fi
    # Picks up an edited unit file; a no-op when nothing changed.
    "${SUDO[@]}" systemctl daemon-reload
    echo "==> starting ${SERVICE}.service"
    if ! "${SUDO[@]}" systemctl start "$SERVICE.service"; then
        echo "error: systemctl start ${SERVICE}.service failed" >&2
        "${SUDO[@]}" systemctl status "$SERVICE.service" --no-pager --lines=15 >&2
        return 1
    fi
    for i in $(seq "$START_WAIT"); do
        "${SUDO[@]}" systemctl is-active --quiet "$SERVICE.service" && break
        sleep 1
    done
    if ! "${SUDO[@]}" systemctl is-active --quiet "$SERVICE.service"; then
        echo "error: ${SERVICE}.service is not active after ${START_WAIT}s" >&2
        "${SUDO[@]}" systemctl status "$SERVICE.service" --no-pager --lines=15 >&2
        return 1
    fi
    return 0
}

report() {
    local pid
    pid=$("${SUDO[@]}" systemctl show -p MainPID --value "$SERVICE.service" 2>/dev/null)
    echo
    echo "==> ${SERVICE}.service is active, MainPID ${pid:-unknown}"
    "${SUDO[@]}" systemctl show -p ActiveEnterTimestamp,DurationStartSec --value --no-pager "$SERVICE.service" 2>/dev/null | sed 's/^/    /'
    # The ingest thread binds the port a second or two after the unit is active,
    # so poll rather than reading the state the instant systemctl returns.
    if wait_for_port; then
        echo "    weather station port ${WS_PORT}: listening"
    else
        echo "    weather station port ${WS_PORT}: not listening after ${PORT_WAIT}s (check daemon.log)"
    fi
    if [ -f "$UNIT_FILE" ] && [ -f "$REPO_UNIT" ] && ! diff -q "$REPO_UNIT" "$UNIT_FILE" >/dev/null 2>&1; then
        echo
        echo "note: $REPO_UNIT differs from the installed $UNIT_FILE."
        echo "      reinstall with sudo cp, then sudo systemctl daemon-reload."
    fi
    if [ -f "$DAEMON_LOG" ]; then
        echo
        echo "==> last 8 lines of daemon.log:"
        tail -n 8 "$DAEMON_LOG"
    fi
}

if [ ! -f "$PROJECT_DIR/$DAEMON_SCRIPT" ]; then
    echo "error: $PROJECT_DIR/$DAEMON_SCRIPT not found" >&2
    exit 1
fi

stop_unit || true
kill_strays || exit 1
start_unit || exit 1
report
echo
echo "tail -f daemon.log agent.log    # from $PROJECT_DIR, both files are uncapped"
