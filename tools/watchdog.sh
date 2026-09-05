#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# FunPayCardinal watchdog for the PUBG UC Spark plugin.
#
# Restarts Cardinal (inside a `screen` session) in TWO situations:
#   1. the process is not running at all;
#   2. the process IS running but its FunPay runner has STALLED - the plugin's
#      heartbeat file has not been updated for STALL_MINUTES. This is the real
#      failure we saw: the process/screen stay alive, but the poll thread hangs
#      on a dead socket after a network flap, so no orders/messages arrive and
#      nothing is logged. `pgrep` alone cannot see this - the heartbeat can.
#
# On restart it creates the plugin's backfill trigger file, so the plugin scans
# for and recovers orders missed during the downtime (BACKFILL_MODE=watchdog).
# A manual restart you do yourself has no trigger and will NOT scan.
#
# Install (run every minute) via cron:
#     * * * * * /home/root/FunPayCardinal/plugins/pubg_uc_spark/tools/watchdog.sh >> /home/root/fpc_watchdog.log 2>&1
#
# Adjust the paths / thresholds below to your server.
# ---------------------------------------------------------------------------
set -u

# --- Config (edit to match your server) ------------------------------------
FPC_DIR="/home/root/FunPayCardinal"
PYTHON="/home/root/pyvenv311/bin/python"
SCREEN_NAME="fpc"                       # `screen -S fpc` session name
PROC_PATTERN="pyvenv311/bin/python.*main.py"   # identifies the Cardinal process
PLUGIN_DIR="${FPC_DIR}/plugins/pubg_uc_spark"
HEARTBEAT_FILE="${PLUGIN_DIR}/.heartbeat"       # must match HEARTBEAT_FILE
TRIGGER_FILE="${PLUGIN_DIR}/.backfill_request"  # must match BACKFILL_TRIGGER_FILE
CONTROL_FILE="${PLUGIN_DIR}/.watchdog"          # must match WATCHDOG_CONTROL_FILE
# Restart if the runner produced no events for this long (process alive). For a
# busy shop 15-20 min of silence means a stalled runner; raise it if quiet
# periods cause needless restarts, lower it to react faster. Overridden live by
# the /uc_watchdog Telegram command via CONTROL_FILE.
STALL_MINUTES=15
# ---------------------------------------------------------------------------

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Runtime control from /uc_watchdog (enable/disable + stall threshold).
if [ -f "${CONTROL_FILE}" ]; then
    wd_enabled=$(grep -E '^WATCHDOG_ENABLED=' "${CONTROL_FILE}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -dc '0-9')
    wd_stall=$(grep -E '^STALL_MINUTES=' "${CONTROL_FILE}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -dc '0-9')
    if [ "${wd_enabled:-1}" = "0" ]; then
        exit 0   # watchdog disabled by admin
    fi
    if [ -n "${wd_stall}" ] && [ "${wd_stall}" -gt 0 ]; then
        STALL_MINUTES="${wd_stall}"
    fi
fi

restart_needed=false
reason=""

if ! pgrep -f "${PROC_PATTERN}" >/dev/null 2>&1; then
    restart_needed=true
    reason="process not running"
elif [ -f "${HEARTBEAT_FILE}" ]; then
    hb=$(tr -dc '0-9' < "${HEARTBEAT_FILE}" 2>/dev/null)
    if [ -n "${hb}" ]; then
        age=$(( $(date +%s) - hb ))
        if [ "${age}" -gt $(( STALL_MINUTES * 60 )) ]; then
            restart_needed=true
            reason="runner stalled (no events for ${age}s > ${STALL_MINUTES}m)"
        fi
    fi
fi

if ! ${restart_needed}; then
    exit 0
fi

echo "$(ts) [watchdog] Restarting Cardinal: ${reason}"

# Signal the plugin to recover orders missed during the downtime.
: > "${TRIGGER_FILE}" 2>/dev/null || echo "$(ts) [watchdog] WARN: cannot write ${TRIGGER_FILE}"

# Stop the (possibly hung) process and any stale screen session with our name.
pkill -f "${PROC_PATTERN}" >/dev/null 2>&1 || true
screen -S "${SCREEN_NAME}" -X quit >/dev/null 2>&1 || true
sleep 2
pkill -9 -f "${PROC_PATTERN}" >/dev/null 2>&1 || true   # force-kill if still up

cd "${FPC_DIR}" || { echo "$(ts) [watchdog] ERROR: cd ${FPC_DIR} failed"; exit 1; }
screen -dmS "${SCREEN_NAME}" "${PYTHON}" main.py

sleep 3
if pgrep -f "${PROC_PATTERN}" >/dev/null 2>&1; then
    echo "$(ts) [watchdog] Cardinal restarted OK"
else
    echo "$(ts) [watchdog] ERROR: restart did not bring Cardinal up"
fi
