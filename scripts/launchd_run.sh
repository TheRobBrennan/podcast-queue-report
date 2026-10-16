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

log "TICK"
if python3 flow.py tick; then
  log "OK"
else
  log "FAIL: flow.py tick exited $?"
  exit 1
fi
