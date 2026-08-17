import sqlite3, datetime, json, os, sys, glob, subprocess
from zoneinfo import ZoneInfo

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

def _load_dotenv():
    """Minimal .env loader (no external dependency). Reads KEY=VALUE lines
    from a .env file next to this script and sets them in os.environ,
    without overriding any variable already set in the real environment."""
    env_path = os.path.join(SCRIPT_DIR, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value

_load_dotenv()

def _resolve_db_path():
    env_path = os.environ.get("PODCASTS_DB_PATH")
    if env_path and os.path.exists(os.path.expanduser(env_path)):
        return os.path.expanduser(env_path)
    standard = os.path.expanduser(
        "~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite"
    )
    if os.path.exists(standard):
        return standard
    # Fallback for Claude Cowork's sandboxed bash, where the real Mac path
    # above isn't directly reachable and the folder is bind-mounted instead.
    for candidate in glob.glob("/sessions/*/mnt/Documents/MTLibrary.sqlite"):
        return candidate
    return standard  # doesn't exist; downstream sqlite3.connect will raise clearly

DB_PATH = _resolve_db_path()
LOCAL_TZ = ZoneInfo(os.environ.get("REPORT_TIMEZONE", "UTC"))
REPORT_EMAIL = os.environ.get("REPORT_EMAIL", "")
REPORT_PHONE = os.environ.get("REPORT_PHONE", "")
REPORT_SIGNOFF_NAME = os.environ.get("REPORT_SIGNOFF_NAME", "")
REPORT_LABEL = os.environ.get("REPORT_LABEL", "Podcasts Report")
REPORT_EMAIL_CLIENT = os.environ.get("REPORT_EMAIL_CLIENT", "")

STATE_PATH = os.path.join(SCRIPT_DIR, ".podcast_skill_state.json")
CORE_DATA_EPOCH = 978307200

def cd_to_dt(v):
    return datetime.datetime.utcfromtimestamp(v + CORE_DATA_EPOCH)

def dt_to_cd(d):
    return (d - datetime.datetime(2001,1,1)).total_seconds()

# ---------------------------------------------------------------------------
# Human-friendly duration formatting, e.g.:
#   "2 hrs 38 mins 12 seconds"
#   "1 day 2 hrs 38 mins 12 seconds"
#   "3 weeks 1 day 2 hrs 38 mins 12 seconds"
#   "1 month 2 weeks 1 day 2 hrs 38 mins 12 seconds"
#   "2 years 1 month 2 weeks 1 day 2 hrs 38 mins 12 seconds"
# Only units with a nonzero value are shown. Approximation: 1 year = 365
# days, 1 month = 30 days, 1 week = 7 days (calendar-exact breakdowns don't
# make sense for a rolling duration like "total listening time").
# ---------------------------------------------------------------------------
_DURATION_UNITS = [
    ("year", 365 * 86400),
    ("month", 30 * 86400),
    ("week", 7 * 86400),
    ("day", 86400),
    ("hr", 3600),
    ("min", 60),
    ("second", 1),
]

def fmt_duration(seconds):
    seconds = int(round(seconds or 0))
    if seconds <= 0:
        return "0 seconds"
    parts = []
    remaining = seconds
    for name, size in _DURATION_UNITS:
        value = remaining // size
        remaining -= value * size
        if value > 0:
            label = name if value == 1 else (f"{name}s" if name != "hr" else "hrs")
            if name == "hr":
                label = "hr" if value == 1 else "hrs"
            # Non-breaking space between the number and its unit so a line
            # wrap never lands mid-pair (e.g. "1" alone on one line, "hr" on
            # the next) — wraps can still happen *between* unit groups.
            parts.append(f"{fmt_num(value)} {label}")
    return " ".join(parts)

def fmt_num(n):
    return f"{int(n):,}"

def fmt_days_behind(seconds, has_queue):
    """Returns (amount, phrase) for how far behind the oldest unplayed
    episode is. `amount` is fmt_duration's full-precision span alone
    ("12 hrs 41 mins 3 seconds") or None when there's nothing to catch up
    on; `phrase` is the ready-to-print form ("12 hrs 41 mins 3 seconds
    behind" / "current as of today"). The letter grade still buckets by
    whole days — this is only how the span is worded."""
    if not has_queue or seconds < 60:
        return None, "current as of today"
    amount = fmt_duration(seconds)
    return amount, f"{amount} behind"


def get_now_playing(cur):
    """Queries macOS's system-wide Now Playing info (via the `nowplaying-cli`
    Homebrew tool, a thin wrapper around the private MediaRemote framework)
    for whatever's actively playing. Returns None unless Podcasts.app itself
    is the current player and it's actually playing (not just paused) —
    the SQLite library alone can't tell us that, only playhead position.

    nowplaying-cli's title AND elapsed/duration are unreliable for some
    podcasts (observed with My First Million): the title is a live
    per-segment/chapter string that changes every few seconds ("Intro —
    ...", "Hark, a human in a box — ...", "zero constraints — ..." — all
    for the same episode) and never matches the stored episode title, and
    kMRMediaRemoteNowPlayingInfoElapsedTime read back as 0 on every poll
    across several minutes of real playback — it isn't tracking actual
    position for this kind of chapter-shifting Now Playing info. Once we
    know which podcast (artist) is playing, we instead look up that
    podcast's currently-playing episode directly via ZPLAYSTATE=1 to get
    its stable title, store links, and the library's own ZPLAYHEAD/
    ZDURATION for elapsed/remaining — nowplaying-cli is only trusted for
    confirming playback is actually active, and as a fallback if that DB
    lookup comes up empty."""
    try:
        result = subprocess.run(
            ["nowplaying-cli", "get-raw"], capture_output=True, text=True, timeout=3
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if info.get("kMRMediaRemoteNowPlayingInfoClientBundleIdentifier") != "com.apple.podcasts":
        return None
    if (info.get("kMRMediaRemoteNowPlayingInfoPlaybackRate") or 0) <= 0:
        return None

    title = info.get("kMRMediaRemoteNowPlayingInfoTitle")
    if not title:
        return None
    podcast = info.get("kMRMediaRemoteNowPlayingInfoArtist") or ""
    duration = info.get("kMRMediaRemoteNowPlayingInfoDuration") or 0
    elapsed = info.get("kMRMediaRemoteNowPlayingInfoElapsedTime") or 0
    remaining = max(duration - elapsed, 0)

    display_title = title
    episode_url = None
    podcast_url = None
    cur.execute(
        "select e.ZTITLE, p.ZSTORECLEANURL, e.ZSTORETRACKID, e.ZPLAYHEAD, e.ZDURATION "
        "from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK "
        "where p.ZTITLE = ? and e.ZPLAYSTATE = 1 limit 1",
        (podcast,),
    )
    row = cur.fetchone()
    if row:
        db_title, pod_url, track_id, db_playhead, db_duration = row
        display_title = db_title or title
        podcast_url = pod_url
        episode_url = f"{pod_url}?i={int(track_id)}" if pod_url and track_id else pod_url
        if db_duration:
            elapsed = db_playhead or 0
            duration = db_duration
            remaining = max(duration - elapsed, 0)

    return {
        "title": display_title,
        "podcast": podcast,
        "episode_url": episode_url,
        "podcast_url": podcast_url,
        "elapsed_seconds": elapsed,
        "duration_seconds": duration,
        "elapsed_fmt": fmt_duration(elapsed),
        "duration_fmt": fmt_duration(duration),
        "remaining_fmt": fmt_duration(remaining),
    }

def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

LATEST_EPISODES_LIMIT = 12

def get_unplayed_queue(cur, now_dt, limit=LATEST_EPISODES_LIMIT):
    """Matches Apple's own "Latest Episodes" view: a rolling window of the
    `limit` most recent unplayed episodes across subscribed podcasts, one
    per podcast (its single newest unplayed episode), sorted by publish
    date. Two earlier heuristics were tried and disproven against actual
    screenshots of the Latest Episodes view:
      1. A 20-hour publish-gap cluster — wrong because shows published well
         over 20h apart were still both present, one per show.
      2. Every unplayed episode, one per podcast, uncapped — wrong because
         it surfaces episodes from long-dormant subscriptions going back
         years; the real view only ever showed the ~12 most recent.
    The count of 12 was confirmed by counting forward from a screenshot
    where the *oldest* item at the top of the list (oldest-first sort
    setting) matched exactly the 12th-most-recent unplayed episode across
    all subscribed shows — not a fixed time window, since two slightly
    older unplayed episodes existed just outside that count and were
    excluded."""
    now_cd = dt_to_cd(now_dt)
    cur.execute('''
        select e.ZTITLE, p.ZTITLE, e.ZDURATION, e.ZPUBDATE, e.ZPLAYHEAD,
               e.ZSTORETRACKID, p.ZSTORECLEANURL, p.Z_PK
        from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK
        where e.ZUNPLAYEDTAB=1 and p.ZSUBSCRIBED=1 and e.ZPUBDATE <= ?
        order by e.ZPUBDATE desc
    ''', (now_cd,))
    rows = cur.fetchall()
    seen_podcasts = set()
    queue = []
    for title, pod, dur, pub, playhead, track_id, pod_url, pod_pk in rows:
        if pod_pk in seen_podcasts:
            continue
        seen_podcasts.add(pod_pk)
        d = cd_to_dt(pub)
        episode_url = f"{pod_url}?i={int(track_id)}" if pod_url and track_id else pod_url
        queue.append({
            "title": title, "podcast": pod, "duration": dur or 0, "pubdate": d,
            "playhead": playhead or 0, "episode_url": episode_url, "podcast_url": pod_url,
        })
        if len(queue) == limit:
            break
    return queue

def grade_for_days(days_behind):
    if days_behind <= 0.05:
        return "A+" if days_behind <= 0 else "A"
    buckets = [
        (1, "A-"), (2, "B+"), (3, "B"), (4, "B-"),
        (5, "C+"), (6, "C"), (7, "C-"),
        (8, "D+"), (9, "D"), (10, "D-"),
    ]
    d = int(days_behind)
    for threshold, grade in buckets:
        if d <= threshold:
            return grade
    return "F"

def get_played_stats(cur, since_dt, until_dt):
    since_cd = dt_to_cd(since_dt)
    until_cd = dt_to_cd(until_dt)
    cur.execute('''
        select e.ZTITLE, p.ZTITLE, e.ZDURATION, e.ZLASTDATEPLAYED
        from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK
        where p.ZSUBSCRIBED=1 and e.ZLASTDATEPLAYED >= ? and e.ZLASTDATEPLAYED < ?
        order by e.ZLASTDATEPLAYED desc
    ''', (since_cd, until_cd))
    rows = cur.fetchall()
    total = sum((r[2] or 0) for r in rows)
    return {"count": len(rows), "total_seconds": total, "episodes": rows}

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)

EMOJI_POOL = ["🐷","🐰","🚙","🚗","🚐","🚘","🐻","🦈","🐸","🦄","🚁","🐐","🧌","🐵",
              "🦶","🦌","🐘","🦬","🐔","🐓","🏝️"]

def emoji_header():
    """Every member of the household, in order, every time — no sampling.
    The header is the whole cast, so nobody gets left out of a report."""
    return "".join(EMOJI_POOL)

def main():
    now = datetime.datetime.utcnow()
    con = connect()
    cur = con.cursor()

    # --- Unplayed queue / grade ---
    queue = get_unplayed_queue(cur, now)
    queue_total = sum(e["duration"] for e in queue)
    if queue:
        oldest = min(e["pubdate"] for e in queue)
        behind_seconds = (now - oldest).total_seconds()
        days_behind = behind_seconds / 86400
    else:
        oldest = None
        behind_seconds = 0
        days_behind = 0
    grade = grade_for_days(days_behind) if queue else "A+"
    days_behind_amount, days_behind_phrase = fmt_days_behind(behind_seconds, bool(queue))

    # --- Played stats windows ---
    state = load_state()
    last_run_iso = state.get("last_run_iso")
    last_run_dt = datetime.datetime.fromisoformat(last_run_iso) if last_run_iso else None

    # Day boundaries computed in local time, then converted back to naive UTC
    # (the DB timestamps / our `now` are naive UTC throughout this script).
    now_local = now.replace(tzinfo=datetime.timezone.utc).astimezone(LOCAL_TZ)
    today_start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_start_local = today_start_local - datetime.timedelta(days=1)
    today_start = today_start_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    yesterday_start = yesterday_start_local.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    week_start = now - datetime.timedelta(days=7)
    month_start = now - datetime.timedelta(days=30)
    all_time_start = datetime.datetime(2000,1,1)

    windows = {}
    if last_run_dt:
        windows["since_last_run"] = get_played_stats(cur, last_run_dt, now)
    windows["yesterday"] = get_played_stats(cur, yesterday_start, today_start)
    windows["past_week"] = get_played_stats(cur, week_start, now)
    windows["past_month"] = get_played_stats(cur, month_start, now)
    windows["all_time"] = get_played_stats(cur, all_time_start, now)

    # queue is newest-first (needed for the gap-detection walk above);
    # reverse so the oldest / "up next" episode is first, matching how
    # Podcasts itself orders the Latest Episodes view.
    queue_oldest_first = list(reversed(queue))

    up_next = queue_oldest_first[0] if queue_oldest_first else None
    up_next_remaining = None
    if up_next:
        up_next_remaining = max((up_next["duration"] or 0) - (up_next["playhead"] or 0), 0)

    result = {
        "generated_at": now.isoformat(),
        "last_run_iso": last_run_iso,
        "queue": {
            "count": len(queue),
            "count_fmt": fmt_num(len(queue)),
            "total_seconds": queue_total,
            "total_fmt": fmt_duration(queue_total),
            "oldest_date": oldest.isoformat() if oldest else None,
            "days_behind": round(days_behind, 2),
            "days_behind_amount": days_behind_amount,
            "days_behind_phrase": days_behind_phrase,
            "grade": grade,
            "up_next_remaining_fmt": fmt_duration(up_next_remaining) if up_next else None,
            "episodes": [
                {"title": e["title"], "podcast": e["podcast"], "duration_fmt": fmt_duration(e["duration"]),
                 "pubdate": e["pubdate"].isoformat(), "episode_url": e["episode_url"],
                 "podcast_url": e["podcast_url"]}
                for e in queue_oldest_first
            ],
        },
        "played": {
            k: {"count": v["count"], "count_fmt": fmt_num(v["count"]),
                "total_seconds": v["total_seconds"], "total_fmt": fmt_duration(v["total_seconds"])}
            for k, v in windows.items()
        },
        "now_playing": get_now_playing(cur),
        "emoji_header": emoji_header(),
        "config": {
            "email": REPORT_EMAIL,
            "phone": REPORT_PHONE,
            "signoff_name": REPORT_SIGNOFF_NAME,
            "label": REPORT_LABEL,
            "email_client": REPORT_EMAIL_CLIENT,
            "timezone": os.environ.get("REPORT_TIMEZONE", "UTC"),
        },
    }

    print(json.dumps(result, indent=2))

    # update state
    save_state({"last_run_iso": now.isoformat()})

if __name__ == "__main__":
    main()
