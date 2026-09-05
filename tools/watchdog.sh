#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# FunPayCardinal watchdog for the PUBG UC Spark plugin.
#
# Keeps Cardinal running inside a `screen` session. If the process is not alive
# it creates the plugin's backfill trigger file and (re)starts Cardinal - so on
# that restart the plugin scans for and recovers orders missed during the
# downtime (BACKFILL_MODE=watchdog). A manual restart you do yourself has no
# trigger, so it will NOT scan.
#
# Install (run every minute) via cron:
#     * * * * * /home/root/FunPayCardinal/plugins/pubg_uc_spark/tools/watchdog.sh >> /home/root/fpc_watchdog.log 2>&1
#
# Adjust the paths below to your server if they differ.
# ---------------------------------------------------------------------------
set -u

# --- Config (edit to match your server) ------------------------------------
FPC_DIR="/home/root/FunPayCardinal"
PYTHON="/home/root/pyvenv311/bin/python"
SCREEN_NAME="fpc"                       # `screen -S fpc` session name
# Pattern that uniquely identifies the running Cardinal process.
PROC_PATTERN="pyvenv311/bin/python.*main.py"
# Backfill trigger file the plugin consumes on startup (must match
# BACKFILL_TRIGGER_FILE; this is the default location next to the package).
TRIGGER_FILE="${FPC_DIR}/plugins/pubg_uc_spark/.backfill_request"
# ---------------------------------------------------------------------------

ts() { date '+%Y-%m-%d %H:%M:%S'; }

# Already running? Nothing to do.
if pgrep -f "${PROC_PATTERN}" >/dev/null 2>&1; then
    exit 0
fi

echo "$(ts) [watchdog] Cardinal not running -> restarting (with backfill trigger)"

# Signal the plugin to recover orders missed during the downtime.
: > "${TRIGGER_FILE}" 2>/dev/null || echo "$(ts) [watchdog] WARN: cannot write ${TRIGGER_FILE}"

# Wipe any dead screen session with the same name, then start a fresh one.
screen -S "${SCREEN_NAME}" -X quit >/dev/null 2>&1 || true
cd "${FPC_DIR}" || { echo "$(ts) [watchdog] ERROR: cd ${FPC_DIR} failed"; exit 1; }
screen -dmS "${SCREEN_NAME}" "${PYTHON}" main.py

sleep 3
if pgrep -f "${PROC_PATTERN}" >/dev/null 2>&1; then
    echo "$(ts) [watchdog] Cardinal restarted OK"
else
    echo "$(ts) [watchdog] ERROR: restart did not bring Cardinal up"
fi
