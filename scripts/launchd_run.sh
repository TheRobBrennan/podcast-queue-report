#!/bin/zsh
# Local scheduled run of the podcast queue report (launchd agent
# ai.sploosh.podcast-queue-report). Discord + email - see CLAUDE.md.
#
# Guards against the back-to-back-runs problem: launchd coalesces missed
# StartCalendarInterval firings into a single run on wake, and MIN_GAP_MINUTES
# below is a second belt for any case where it doesn't.

set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$HOME/Library/Logs/podcast-queue-report"
STAMP_FILE="$LOG_DIR/.last_success"
MIN_GAP_MINUTES=${MIN_GAP_MINUTES:-60}

export PATH="/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$LOG_DIR"
log() { print -r -- "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

cd "$REPO" || { log "FAIL: repo not found at $REPO"; exit 1; }

if [[ ! -f .env ]]; then
  log "FAIL: .env missing in $REPO - setup incomplete"
  exit 1
fi

# Skip if a successful run finished less than MIN_GAP_MINUTES ago.
if [[ -f "$STAMP_FILE" ]]; then
  last=$(cat "$STAMP_FILE")
  now=$(date +%s)
  gap=$(( (now - last) / 60 ))
  if (( gap < MIN_GAP_MINUTES )); then
    log "SKIP: last success was ${gap}m ago (< ${MIN_GAP_MINUTES}m)"
    exit 0
  fi
fi

log "START"
# DRY_RUN=1 exercises the DB query only, so the agent can be verified
# without posting to Discord or sending email.
if [[ "${DRY_RUN:-0}" == "1" ]]; then
  if make run; then
    log "OK (dry run): queried the Podcasts DB, skipped Discord + email"
    exit 0
  else
    log "FAIL (dry run): make run exited $?"
    exit 1
  fi
fi

# `make cron` queries the Podcasts DB exactly once, then posts to Discord
# and sends the email report. Do not use `make all` - it drags in the sms
# target too.
if make cron; then
  date +%s > "$STAMP_FILE"
  log "OK: posted to Discord and sent email"
else
  log "FAIL: make cron exited $?"
  exit 1
fi
