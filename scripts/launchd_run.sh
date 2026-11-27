#!/bin/zsh
# Hourly scheduler entry point (launchd agent ai.sploosh.podcast-queue-report).
#
# The agent fires at :06 every hour, which means the same thing in every time
# zone. What actually runs is decided at run time by `flow.py tick`, using the
# Mac's local clock: it takes a fallback morning snapshot (11:00-17:00, only
# if none exists yet today) or the evening snapshot (after 22:00, only if
# none exists yet) and otherwise does nothing. The real triggers - /start-day,
# the Dash start and end, /close-day - call `make snapshot LABEL=...`
# themselves, so a day with all of those never needs a fallback.
#
# DRY_RUN=1 prints what would run without querying, posting, or emailing.

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/podcast-queue-report"

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$LOG_DIR"
log() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

cd "$REPO" || { log "FAIL: repo not found at $REPO"; exit 1; }

if [[ ! -f .env ]]; then
  log "FAIL: .env missing in $REPO - setup incomplete"
  exit 1
fi

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  python3 flow.py tick --dry-run
  exit $?
fi

# A tick normally finishes in a couple of minutes. launchd will not start a new
# run while the last one is alive, so a hung one silently stops every later
# hourly reading (it did, for two days, from 2026-10-28). Cap it instead.
#
# Do not wrap the tick in coreutils `timeout`: macOS then treats gtimeout, not
# Python, as the program asking for the Podcasts database, Python's Full Disk
# Access no longer applies, and every read fails with "unable to open database
# file" (it did from 2026-10-30).
TICK_TIMEOUT="${TICK_TIMEOUT:-600}"

log "TICK"
python3 flow.py tick &
tick=$!
{ sleep "$TICK_TIMEOUT"; kill "$tick"; sleep 10; kill -9 "$tick"; } >/dev/null 2>&1 &
watchdog=$!
wait "$tick"
rc=$?
kill "$watchdog" 2>/dev/null
if [[ $rc -eq 0 ]]; then
  log "OK"
elif [[ $rc -eq 143 || $rc -eq 137 ]]; then
  log "FAIL: flow.py tick timed out after ${TICK_TIMEOUT}s and was stopped"
  exit 1
else
  log "FAIL: flow.py tick exited $rc"
  exit 1
fi
