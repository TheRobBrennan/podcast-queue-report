import sqlite3, datetime, json, os, sys, random, glob
from zoneinfo import ZoneInfo

DB_PATH = "/sessions/gallant-vigilant-albattani/mnt/Documents/MTLibrary.sqlite"
LOCAL_TZ = ZoneInfo("America/Los_Angeles")  # confirmed via Mac menu bar clock on 2026-08-15

def _find_workspace_folder():
    # The connected " Podcasts" workspace folder may contain a non-ASCII
    # leading character depending on how it was named on disk; resolve it
    # dynamically instead of hardcoding the exact bytes.
    mnt = "/sessions/gallant-vigilant-albattani/mnt"
    for name in os.listdir(mnt):
        if name.strip().endswith("Podcasts") and name != "Documents":
            return os.path.join(mnt, name)
    return os.path.join(mnt, "outputs")  # fallback

STATE_PATH = os.path.join(_find_workspace_folder(), ".podcast_skill_state.json")
CORE_DATA_EPOCH = 978307200

def cd_to_dt(v):
    return datetime.datetime.utcfromtimestamp(v + CORE_DATA_EPOCH)

def dt_to_cd(d):
    return (d - datetime.datetime(2001,1,1)).total_seconds()

def fmt_hm(seconds):
    seconds = int(seconds or 0)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h and m: return f"{h:,}h {m}m"
    if h: return f"{h:,}h"
    return f"{m}m"

def fmt_num(n):
    return f"{int(n):,}"

def connect():
    return sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)

def get_unplayed_queue(cur, now_dt, gap_hours=20):
    now_cd = dt_to_cd(now_dt)
    cur.execute('''
        select e.ZTITLE, p.ZTITLE, e.ZDURATION, e.ZPUBDATE
        from ZMTEPISODE e join ZMTPODCAST p on e.ZPODCAST = p.Z_PK
        where e.ZUNPLAYEDTAB=1 and p.ZSUBSCRIBED=1 and e.ZPUBDATE <= ?
        order by e.ZPUBDATE desc
    ''', (now_cd,))
    rows = cur.fetchall()
    cluster = []
    prev_dt = None
    for title, pod, dur, pub in rows:
        d = cd_to_dt(pub)
        if prev_dt is not None:
            gap = (prev_dt - d).total_seconds() / 3600
            if gap > gap_hours:
                break
        cluster.append({"title": title, "podcast": pod, "duration": dur or 0, "pubdate": d})
        prev_dt = d
    return cluster

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
              "🦶","🦌","🐘","🦬","🐔","🐓","🏝️","🦖","🐙","🚀","🦥","🐨","🚂","🦩",
              "🐳","🦁","🐢","🚜","🦒","🐝","🛸","🐊"]

def emoji_header(n=15):
    return "".join(random.choices(EMOJI_POOL, k=n))

def main():
    now = datetime.datetime.utcnow()
    con = connect()
    cur = con.cursor()

    # --- Unplayed queue / grade ---
    queue = get_unplayed_queue(cur, now)
    queue_total = sum(e["duration"] for e in queue)
    if queue:
        oldest = min(e["pubdate"] for e in queue)
        days_behind = (now - oldest).total_seconds() / 86400
    else:
        oldest = None
        days_behind = 0
    grade = grade_for_days(days_behind) if queue else "A+"

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

    result = {
        "generated_at": now.isoformat(),
        "last_run_iso": last_run_iso,
        "queue": {
            "count": len(queue),
            "count_fmt": fmt_num(len(queue)),
            "total_seconds": queue_total,
            "total_hm": fmt_hm(queue_total),
            "oldest_date": oldest.isoformat() if oldest else None,
            "days_behind": round(days_behind, 2),
            "grade": grade,
            "episodes": [
                {"title": e["title"], "podcast": e["podcast"], "duration_hm": fmt_hm(e["duration"]),
                 "pubdate": e["pubdate"].isoformat()}
                for e in queue_oldest_first
            ],
        },
        "played": {
            k: {"count": v["count"], "count_fmt": fmt_num(v["count"]),
                "total_seconds": v["total_seconds"], "total_hm": fmt_hm(v["total_seconds"])}
            for k, v in windows.items()
        },
        "emoji_header": emoji_header(),
    }

    print(json.dumps(result, indent=2))

    # update state
    save_state({"last_run_iso": now.isoformat()})

if __name__ == "__main__":
    main()
