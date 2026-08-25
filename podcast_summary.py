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

STATE_PATH = os.path.join(SCRIPT_DIR, ".podcast_skill_state.json")
CORE_DATA_EPOCH = 978307200

def cd_to_dt(v):
    return datetime.datetime.fromtimestamp(v + CORE_DATA_EPOCH, datetime.UTC).replace(tzinfo=None)

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

def artwork_url(template, size=300):
    """Fills in Apple's {w}x{h}bb.{f} artwork template (ZARTWORKTEMPLATEURL)
    at a given square size, requesting jpg regardless of the source format —
    the CDN transcodes on the fly. Returns None if there's no template
    (a podcast added before Apple started populating this column, or one
    whose feed never supplied artwork)."""
    if not template:
        return None
    return (template.replace("{w}", str(size))
                     .replace("{h}", str(size))
                     .replace("{f}", "jpg"))

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
    artwork = None
    cur.execute(
        "select e.ZTITLE, p.ZSTORECLEANURL, e.ZSTORETRACKID, e.ZPLAYHEAD, e.ZDURATION, "
        "p.ZARTWORKTEMPLATEURL "
        "from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK "
        "where p.ZTITLE = ? and e.ZPLAYSTATE = 1 limit 1",
        (podcast,),
    )
    row = cur.fetchone()
    if row:
        db_title, pod_url, track_id, db_playhead, db_duration, artwork_template = row
        display_title = db_title or title
        podcast_url = pod_url
        episode_url = f"{pod_url}?i={int(track_id)}" if pod_url and track_id else pod_url
        artwork = artwork_url(artwork_template)
        if db_duration:
            elapsed = db_playhead or 0
            duration = db_duration
            remaining = max(duration - elapsed, 0)

    if artwork is None and podcast:
        # The ZPLAYSTATE=1 lookup above can come up empty (e.g. mid-chapter
        # title drift never matched a stored episode row) - fall back to
        # the podcast's own artwork by title alone so Now Playing still
        # gets an image instead of going without.
        cur.execute("select ZARTWORKTEMPLATEURL from ZMTPODCAST where ZTITLE = ? limit 1", (podcast,))
        fallback = cur.fetchone()
        if fallback:
            artwork = artwork_url(fallback[0])

    return {
        "title": display_title,
        "podcast": podcast,
        "episode_url": episode_url,
        "podcast_url": podcast_url,
        "artwork_url": artwork,
        "elapsed_seconds": elapsed,
        "duration_seconds": duration,
        "elapsed_fmt": fmt_duration(elapsed),
        "duration_fmt": fmt_duration(duration),
        "remaining_fmt": fmt_duration(remaining),
    }

def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

LATEST_EPISODES_WINDOW_DAYS = 30

def get_unplayed_queue(cur, now_dt, window_days=LATEST_EPISODES_WINDOW_DAYS):
    """The unplayed queue: every never-started episode published in the
    last `window_days` days across subscribed podcasts (no per-podcast
    dedup - if a show has published multiple unplayed episodes inside the
    window, all of them appear), PLUS any episode that has been started
    but not finished, which stays in the view no matter how far outside
    the window its publish date has fallen.

    Matches Apple's own "Latest Episodes" view - it has an explicit
    "Sort By" menu with a time window (1 Week / 2 Weeks / 1 Month / All),
    screenshotted directly on Rob's Mac and confirmed set to "1 Month".
    It is a date filter, not an item-count cap. Getting here took several
    wrong turns, each "confirmed" against one screenshot and disproven by
    the next:
      1. A 20-hour publish-gap cluster — wrong because shows published well
         over 20h apart were still both present, one per show.
      2. Every unplayed episode, one per podcast, uncapped — dismissed at
         the time for surfacing long-dormant subscriptions, but this was
         actually closer to right than what replaced it - it just needed a
         time bound, not an item-count one.
      3. A flat count cap (first 12, later corrected to 13) over the
         never-started episodes — repeatedly "confirmed" by counting
         forward from a screenshot, and repeatedly wrong the next time a
         screenshot was taken, because the real UI isn't counting items -
         see the window explanation above.
      4. Pinning *every* started-but-unfinished episode regardless of age —
         wrong in the other direction. This library holds four abandoned
         ZPLAYSTATE=1 episodes last touched between 5 and 26 months ago
         (Ghost Story "The House Next Door", Morbid "Listener Tales 83",
         and two from Feb 2026); none appear in the real view, but pinning
         them all dragged the oldest item back to Oct 2023 — "2 years 9
         months behind", grade F, against the A-/B+ the view actually
         implies.
      5. Requiring e.ZLISTENNOWEPISODE=1 on never-started candidates —
         looked promising (NULL on an episode the real view excluded, set
         on one it included) but disproven by a follow-up screenshot: both
         episodes were actually present simultaneously. The flag was a red
         herring; the count itself was the bug.
      6. A fixed baseline date instead of a rolling window - tried briefly
         per an offhand reading of Rob's "anything old that appears
         unplayed is a bug from my end" as "exclude everything before
         today." Wrong: that comment was about genuinely ancient dormant
         episodes (a Jan 2026 one, a Dec 2024 one), not the previous day's
         episodes - Rob confirmed the 14-item 1-month-window list (which
         includes yesterday's episodes) as correct the moment the count
         dropped to 7. Only per-podcast dedup, confirmed separately, was
         worth keeping from that detour.
      7. Not filtering on entitlement - a never-started, in-window episode
         can still be one Rob can't actually play: e.ZENTITLEMENTSTATE=2 /
         e.ZPRICETYPE='PSUB' marks a paid-subscriber-exclusive bonus
         episode whose metadata Podcasts cached from the feed without Rob
         being entitled to it (caught via screenshot: a National Park
         After Dark Patreon bonus episode counted as a 17th item while the
         real "Latest Episodes" view topped out at 16 - Rob confirmed he
         hadn't played or dismissed it on any device). Apple's own UI
         won't surface content it won't let you play. Every entitled
         episode in the library has ZENTITLEMENTSTATE=0; every PSUB one
         has 2 - no other value appears, so this is a clean exclusion
         rather than another heuristic guess.

    A started episode is pinned only while it is still *active*: its
    ZLASTDATEPLAYED must be no older than the window itself (the oldest
    never-started episode still inside `window_days`), with a 24-hour
    floor so an episode paused yesterday survives a day whose new releases
    are only a few hours old. ZPLAYSTATE alone cannot carry this
    distinction — all five started episodes here are ZPLAYSTATE=1,
    including the abandoned ones. If Rob ever changes his own Sort By
    setting in Podcasts.app, update LATEST_EPISODES_WINDOW_DAYS to match -
    it isn't readable from this SQLite library, only from the app's own
    UI."""
    now_cd = dt_to_cd(now_dt)
    window_cd = dt_to_cd(now_dt - datetime.timedelta(days=window_days))
    cur.execute('''
        select e.ZTITLE, p.ZTITLE, e.ZDURATION, e.ZPUBDATE, e.ZPLAYHEAD,
               e.ZSTORETRACKID, p.ZSTORECLEANURL, p.Z_PK, e.ZPLAYSTATE,
               e.ZLASTDATEPLAYED, p.ZARTWORKTEMPLATEURL
        from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK
        where e.ZUNPLAYEDTAB=1 and p.ZSUBSCRIBED=1 and e.ZPUBDATE <= ?
          and e.ZENTITLEMENTSTATE=0
        order by e.ZPUBDATE desc
    ''', (now_cd,))
    rows = cur.fetchall()

    fresh = []
    started = []
    seen_started = set()
    for title, pod, dur, pub, playhead, track_id, pod_url, pod_pk, playstate, last_played, artwork_template in rows:
        is_started = playstate == 1 or (playhead or 0) > 0
        # Never-started candidates must fall inside the window; started
        # episodes are exempt (pinning has its own recency rule below,
        # independent of the window).
        if not is_started and pub < window_cd:
            continue
        episode_url = f"{pod_url}?i={int(track_id)}" if pod_url and track_id else pod_url
        entry = {
            "title": title, "podcast": pod, "duration": dur or 0, "pubdate": cd_to_dt(pub),
            "playhead": playhead or 0, "in_progress": is_started,
            "last_played": cd_to_dt(last_played) if last_played else None,
            "episode_url": episode_url, "podcast_url": pod_url, "podcast_pk": pod_pk,
            "artwork_url": artwork_url(artwork_template),
        }
        if is_started:
            if pod_pk in seen_started:
                continue
            seen_started.add(pod_pk)
            started.append(entry)
        else:
            # No dedup here - every never-started episode inside the
            # window shows, even multiple from the same podcast.
            fresh.append(entry)

    # A started episode stays only while still active — see the docstring.
    active_cutoff = min(
        [now_dt - datetime.timedelta(days=1)] + [e["pubdate"] for e in fresh]
    )
    active = [
        e for e in started
        if e["last_played"] and e["last_played"] >= active_cutoff
    ]

    queue = active + fresh
    queue.sort(key=lambda e: e["pubdate"], reverse=True)
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
    """Counts episodes *finished* in the window, one row each, crediting the
    episode's full ZDURATION. ZLASTDATEPLAYED alone is not enough: it also
    moves when an episode is merely started and paused, so an episode still
    sitting in the unplayed tab (ZUNPLAYEDTAB=1) has to be excluded or it
    gets counted as a complete listen. That is not hypothetical — the
    in-progress Analog(ue) episode was reported as the entire "since last
    check" figure (1 episode, 1 hr 19 mins 47 seconds) while 21 minutes of
    it were still unplayed, and it was simultaneously missing from the
    queue. The unfinished portion of such an episode is credited later,
    once it is actually finished and leaves the unplayed tab."""
    since_cd = dt_to_cd(since_dt)
    until_cd = dt_to_cd(until_dt)
    cur.execute('''
        select e.ZTITLE, p.ZTITLE, e.ZDURATION, e.ZLASTDATEPLAYED
        from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK
        where p.ZSUBSCRIBED=1 and e.ZLASTDATEPLAYED >= ? and e.ZLASTDATEPLAYED < ?
          and coalesce(e.ZUNPLAYEDTAB, 0) != 1
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
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
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
    # A plain reversed(queue) would also invert the active-vs-fresh
    # tie-break for episodes sharing a pubdate (queue is built as
    # active + fresh, so ties favor the active one) - sort explicitly
    # instead so a same-pubdate started episode still lands ahead of
    # an unstarted one after flipping to oldest-first.
    queue_oldest_first = sorted(queue, key=lambda e: (e["pubdate"], not e["in_progress"]))

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
                 "podcast_url": e["podcast_url"], "in_progress": e["in_progress"],
                 "artwork_url": e["artwork_url"],
                 "remaining_fmt": fmt_duration(max((e["duration"] or 0) - (e["playhead"] or 0), 0))
                                  if e["in_progress"] else None}
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
            "timezone": os.environ.get("REPORT_TIMEZONE", "UTC"),
        },
    }

    print(json.dumps(result, indent=2))

    # update state
    save_state({"last_run_iso": now.isoformat()})

if __name__ == "__main__":
    main()
