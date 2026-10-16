#!/usr/bin/env python3
"""
Daily-flow snapshots for the Podcast Queue Report.

The old schedule ran a full report every two hours by the clock. This
replaces it with a handful of snapshots tied to the shape of the day -
morning, the start and end of a Dash, evening - plus a weekly roll-up. Each
snapshot is one query against the Podcasts DB (same as `make run`), is
appended to a local history file, and the history is what the day-flow
report is built from.

Labels:
    morning     the day starts (/start-day), or the 11:00 fallback
    dash-start  a Dash begins (the /doordash skill)
    dash-end    the Dash ends
    evening     the day closes (/close-day), or the 22:00 fallback
    manual      anything else (make snapshot)

Commands:
    flow.py snapshot --label evening [--quiet] [--open]
    flow.py day  [--date YYYY-MM-DD] [--format chat|html|email]
    flow.py week [--date YYYY-MM-DD] [--format chat|html|email]
    flow.py tick [--dry-run] [--now ISO]   (what launchd runs every hour)

A "day" runs 4:00am to 4:00am local time, so a snapshot taken at 12:30am
still belongs to the day that is closing.

Standard library only, like the rest of the repo.
"""
import argparse
import datetime
import html
import json
import os
import re
import subprocess
import sys

import podcast_summary as ps
import render_report as rr

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_PATH = os.environ.get(
    "PODCAST_HISTORY_PATH", os.path.join(SCRIPT_DIR, ".podcast_history.jsonl")
)
RUN_JSON = os.environ.get("RUN_JSON", "/tmp/podcast_run.json")
LABELS = ("morning", "dash-start", "dash-end", "evening", "manual", "pulse")
LABEL_NAMES = {
    "morning": ("☀️", "Morning"),
    "dash-start": ("🚐", "Dash start"),
    "dash-end": ("🏁", "Dash end"),
    "evening": ("🌙", "Evening"),
    "manual": ("📸", "Check-in"),
    "pulse": ("📸", "Check-in"),
}
_UNIT_SECONDS = {"week": 604800, "day": 86400, "hr": 3600, "min": 60, "second": 1}

DAY_STARTS_HOUR = 4
MORNING_FALLBACK_HOUR = 11
MORNING_FALLBACK_END_HOUR = 17   # past this, a "morning" snapshot would be a lie
EVENING_FALLBACK_HOUR = 22
MIN_GAP_MINUTES = 20
PULSE_EVERY_MINUTES = 55       # a quiet reading about every hour while the Mac is awake
PULSE_FIRST_HOUR, PULSE_LAST_HOUR = 6, 23
REPORTS_DIR = os.path.join(SCRIPT_DIR, "reports")
DISCORD_FLOW_LABELS = ("dash-end", "evening")


# --- history ---------------------------------------------------------------

def parse_utc(iso):
    """The JSON's timestamps are naive UTC."""
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def parse_fmt_seconds(text):
    """Seconds from podcast_summary.fmt_duration text, e.g. '1 hr 4 mins 5 seconds'."""
    total = 0
    for num, unit in re.findall(r"(\d[\d,]*)\s*(week|day|hr|min|second)", (text or "").replace("\xa0", " ")):
        total += int(num.replace(",", "")) * _UNIT_SECONDS[unit]
    return total


def in_progress_episodes(d, top=8):
    """Episodes you have started but not finished: how far through each, and
    how much is left."""
    out = []
    for e in d["queue"].get("episodes", []):
        if not e.get("in_progress") or not e.get("remaining_fmt"):
            continue
        total = parse_fmt_seconds(e.get("duration_fmt"))
        left = parse_fmt_seconds(e.get("remaining_fmt"))
        if total <= 0:
            continue
        pct = max(0, min(100, round(100 * (1 - left / total))))
        out.append({"title": e.get("title"), "podcast": e.get("podcast"), "episode_url": e.get("episode_url"),
                    "podcast_url": e.get("podcast_url"), "artwork_url": e.get("artwork_url"),
                    "pct": pct, "left": e.get("remaining_fmt"), "length": e.get("duration_fmt")})
    out.sort(key=lambda r: -r["pct"])
    return out[:top]


def queue_by_show(d, top=None, episodes=False):
    """The queue grouped by show: episode count, listening time, the oldest
    waiting episode, and the show's artwork and link."""
    shows = {}
    for e in d["queue"].get("episodes", []):
        name = e.get("podcast") or "Unknown"
        row = shows.setdefault(name, {"podcast": name, "count": 0, "seconds": 0, "oldest": None,
                                      "oldest_title": None, "oldest_url": None, "oldest_duration": None,
                                      "artwork_url": None, "podcast_url": None})
        row["count"] += 1
        row["seconds"] += parse_fmt_seconds(e.get("duration_fmt"))
        if episodes:
            row.setdefault("episodes", []).append({
                "title": e.get("title"), "url": e.get("episode_url"),
                "pubdate": e.get("pubdate"), "duration_fmt": e.get("duration_fmt")})
        pub = e.get("pubdate")
        if pub and (row["oldest"] is None or pub < row["oldest"]):
            row["oldest"] = pub
            row["oldest_title"] = e.get("title")
            row["oldest_url"] = e.get("episode_url")
            row["oldest_duration"] = e.get("duration_fmt")
        row["artwork_url"] = row["artwork_url"] or e.get("artwork_url")
        row["podcast_url"] = row["podcast_url"] or e.get("podcast_url")
    for row in shows.values():
        if "episodes" in row:
            row["episodes"].sort(key=lambda x: (x["pubdate"] is None, x["pubdate"] or ""))
    ranked = sort_oldest_first(list(shows.values()))
    return ranked[:top] if top else ranked


def sort_oldest_first(rows):
    """Stalest show first (its oldest unplayed episode is the oldest); shows with
    no date go last, ties by name."""
    return sorted(rows, key=lambda r: (r.get("oldest") is None, r.get("oldest") or "", r["podcast"].lower()))


def snapshot_from_run(d, label, source="live"):
    q = d["queue"]
    since = d["played"]["since_last_run"]
    np = d.get("now_playing") or None
    snap = {
        "ts": d["generated_at"],
        "since": d.get("last_run_iso") or d["generated_at"],
        "label": label,
        "source": source,
        "queue_count": int(q["count"]),
        "queue_seconds": int(round(q["total_seconds"])),
        "behind_seconds": int(round((q.get("days_behind") or 0) * 86400)),
        "grade": q.get("grade"),
        "played_count": int(since["count"]),
        "played_seconds": int(round(since["total_seconds"])),
    }
    if np:
        snap["now_playing"] = {k: np.get(k) for k in (
            "title", "podcast", "episode_url", "podcast_url", "artwork_url",
            "elapsed_seconds", "duration_seconds", "remaining_fmt") if np.get(k) is not None}
    started = in_progress_episodes(d)
    if started:
        snap["in_progress"] = started
    nxt = rr._next_up_episode(d) if d["queue"].get("episodes") else None
    if nxt:
        snap["up_next"] = {k: nxt.get(k) for k in (
            "title", "podcast", "episode_url", "podcast_url", "artwork_url", "duration_fmt")
            if nxt.get(k) is not None}
    shows = queue_by_show(d)
    if shows:
        snap["by_show"] = shows
    return snap


def append_snapshot(snap, path=None):
    with open(path or HISTORY_PATH, "a") as f:
        f.write(json.dumps(snap, sort_keys=True) + "\n")


def load_history(path=None):
    path = path or HISTORY_PATH
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    rows.sort(key=lambda r: r["ts"])
    return rows


def local_dt(snap, tz=None, key="ts"):
    return parse_utc(snap[key]).astimezone(tz or ps.LOCAL_TZ)


def day_of(dt_local):
    """The flow day a local datetime belongs to (days start at 4:00am)."""
    return (dt_local - datetime.timedelta(hours=DAY_STARTS_HOUR)).date()


def day_snapshots(history, day, tz=None):
    return [s for s in history if day_of(local_dt(s, tz)) == day]


# --- analysis --------------------------------------------------------------

def arrivals_seconds(prev, cur):
    """Estimated new content that landed in the queue between two snapshots:
    the change in queue length plus what was played out of it."""
    return max(0, (cur["queue_seconds"] - prev["queue_seconds"]) + cur["played_seconds"])


def episode_progress(prev, cur):
    """How far you moved through the episode that was playing at both readings,
    or None when it wasn't the same episode or the position wasn't recorded.
    Forward is progress; backward means you went back and replayed."""
    a, b = prev.get("now_playing") or {}, cur.get("now_playing") or {}
    if not a.get("title") or a.get("title") != b.get("title") or a.get("podcast") != b.get("podcast"):
        return None
    if a.get("elapsed_seconds") is None or b.get("elapsed_seconds") is None:
        return None
    dur_s = b.get("duration_seconds") or a.get("duration_seconds") or 0

    def pct(x):
        return round(100 * min(x, dur_s) / dur_s) if dur_s > 0 else None
    return {"delta": b["elapsed_seconds"] - a["elapsed_seconds"],
            "from_pct": pct(a["elapsed_seconds"]), "to_pct": pct(b["elapsed_seconds"])}


def arrivals_count(prev, cur):
    """Estimated episodes that landed between two snapshots: the change in
    queue size plus the episodes played out of it."""
    return max(0, (cur["queue_count"] - prev["queue_count"]) + cur["played_count"])


def summarize_day(history, day, tz=None):
    snaps = day_snapshots(history, day, tz)
    out = {"day": day, "snapshots": snaps, "dashes": []}
    if not snaps:
        return out
    first, last = snaps[0], snaps[-1]
    # The first reading's window reaches back before the day's first reading,
    # so listening is counted from that reading on - the same "since" the
    # queue card uses.
    out["listened_seconds"] = sum(s["played_seconds"] for s in snaps[1:])
    out["listened_count"] = sum(s["played_count"] for s in snaps[1:])
    out["queue_start"], out["queue_end"] = first, last
    out["queue_count_delta"] = last["queue_count"] - first["queue_count"]
    out["queue_seconds_delta"] = last["queue_seconds"] - first["queue_seconds"]
    out["behind_delta"] = last["behind_seconds"] - first["behind_seconds"]
    out["arrivals_seconds"] = sum(
        arrivals_seconds(a, b) for a, b in zip(snaps, snaps[1:])
    )
    out["arrivals_count"] = sum(arrivals_count(a, b) for a, b in zip(snaps, snaps[1:]))

    # Dash windows: each dash-start paired with the next dash-end.
    open_start = None
    for s in snaps:
        if s["label"] == "dash-start":
            open_start = s
        elif s["label"] == "dash-end" and open_start is not None:
            window = [x for x in snaps if open_start["ts"] < x["ts"] <= s["ts"]]
            out["dashes"].append({
                "start": open_start,
                "end": s,
                "listened_seconds": sum(x["played_seconds"] for x in window),
                "listened_count": sum(x["played_count"] for x in window),
                "queue_seconds_delta": s["queue_seconds"] - open_start["queue_seconds"],
                "queue_count_delta": s["queue_count"] - open_start["queue_count"],
                "behind_delta": s["behind_seconds"] - open_start["behind_seconds"],
            })
            open_start = None

    # Longest drought: consecutive snapshot windows with nothing played.
    best = None
    run_start = None
    zone = tz or ps.LOCAL_TZ
    day_start = datetime.datetime.combine(
        day, datetime.time(DAY_STARTS_HOUR), tzinfo=zone
    ).astimezone(datetime.timezone.utc)
    for s in snaps[1:]:
        if s["played_seconds"] == 0:
            if run_start is None:
                run_start = s["since"]
            start_dt = max(parse_utc(run_start), day_start)
            span = (parse_utc(s["ts"]) - start_dt).total_seconds()
            if best is None or span > best[0]:
                best = (span, start_dt.isoformat(), s["ts"])
        else:
            run_start = None
    out["drought"] = best

    prev = None
    windows = []
    for sn in snaps:
        windows.append({
            "snap": sn,
            "progress": episode_progress(prev, sn) if prev else None,
            "first": prev is None,
            "played": 0 if prev is None else sn["played_seconds"],
            "arrivals": arrivals_seconds(prev, sn) if prev else None,
            "arrivals_count": arrivals_count(prev, sn) if prev else None,
            "queue_delta": (sn["queue_count"] - prev["queue_count"]) if prev else None,
        })
        prev = sn
    out["windows"] = windows
    heard = [w for w in windows if w["played"] > 0]
    out["biggest_listen"] = max(heard, key=lambda w: w["played"]) if heard else None
    landed = [w for w in windows if w["arrivals"]]
    out["biggest_arrival"] = max(landed, key=lambda w: w["arrivals"]) if landed else None
    out["closed"] = any(sn["label"] == "evening" for sn in snaps)
    out["by_part"] = listening_by_part(windows, tz)
    first_dt, last_dt = parse_utc(first["ts"]), parse_utc(last["ts"])
    out["elapsed_hours"] = (last_dt - first_dt).total_seconds() / 3600
    out["cutoff_snap"] = last
    out["yesterday_listened"], out["recent_average"], out["recent_days"] = day_context(
        history, day, tz, cutoff=day_offset(last, tz))
    started = [sn for sn in snaps if sn.get("in_progress")]
    out["in_progress_snapshot"] = started[-1] if started else None
    shows = [sn for sn in snaps if sn.get("by_show")]
    out["by_show_snapshot"] = shows[-1] if shows else None
    playing = [sn for sn in snaps if sn.get("now_playing")]
    out["now_playing"] = playing[-1]["now_playing"] if playing else None
    out["up_next"] = (playing[-1].get("up_next") if playing else None)
    return out


PARTS = (
    (4, 12, "🌅", "morning", "4am to 12pm"),
    (12, 17, "🌤️", "afternoon", "12pm to 5pm"),
    (17, 22, "🌆", "evening", "5pm to 10pm"),
    (22, 28, "🌙", "late night", "10pm to 4am"),
)


def part_of(dt_local):
    h = dt_local.hour if dt_local.hour >= DAY_STARTS_HOUR else dt_local.hour + 24
    for lo, hi, icon, name, rng in PARTS:
        if lo <= h < hi:
            return icon, name
    return PARTS[-1][2], PARTS[-1][3]


def listening_by_part(windows, tz=None):
    """Seconds listened per part of the day. Each reading's listening is spread
    evenly across the window it covers (since the previous reading), so a
    window that straddles noon is split between morning and afternoon."""
    zone = tz or ps.LOCAL_TZ
    totals = {}
    for w in windows:
        if w.get("first") or w["played"] <= 0:
            continue
        start = parse_utc(w["snap"]["since"]).astimezone(zone)
        end = parse_utc(w["snap"]["ts"]).astimezone(zone)
        span = (end - start).total_seconds()
        if span <= 0:
            continue
        day0 = datetime.datetime.combine(
            day_of(end), datetime.time(DAY_STARTS_HOUR), tzinfo=zone
        )
        for lo, hi, icon, name, rng in PARTS:
            p_start = day0 + datetime.timedelta(hours=lo - DAY_STARTS_HOUR)
            p_end = day0 + datetime.timedelta(hours=hi - DAY_STARTS_HOUR)
            overlap = (min(end, p_end) - max(start, p_start)).total_seconds()
            if overlap > 0:
                row = totals.setdefault(name, {"icon": icon, "name": name, "range": rng, "seconds": 0})
                row["seconds"] += w["played"] * overlap / span
    rows = [dict(r, seconds=int(round(r["seconds"]))) for r in totals.values()]
    return sorted(rows, key=lambda r: -r["seconds"])


def day_offset(snap, tz=None):
    """Seconds into the flow day (which starts at 4am local) a reading was taken."""
    dt = local_dt(snap, tz)
    start = datetime.datetime.combine(day_of(dt), datetime.time(DAY_STARTS_HOUR), tzinfo=dt.tzinfo)
    return (dt - start).total_seconds()


def day_context(history, day, tz=None, cutoff=None):
    """How earlier days compare at the same point in the day. Returns
    (yesterday's listening by `cutoff`, average listening by `cutoff` over the
    earlier days that have data, how many days that average covers); each None
    or 0 when there is nothing to compare with. `cutoff` is seconds into the
    day, so a half-finished day is compared with half-finished days."""
    def listened(d):
        snaps = day_snapshots(history, d, tz)
        if not snaps:
            return None
        return sum(s["played_seconds"] for s in snaps[1:]
                   if cutoff is None or day_offset(s, tz) <= cutoff)
    yesterday = listened(day - datetime.timedelta(days=1))
    prior = [x for x in (listened(day - datetime.timedelta(days=i)) for i in range(1, 8)) if x is not None]
    return yesterday, (sum(prior) / len(prior) if prior else None), len(prior)


def week_days(anchor_day):
    """Monday through anchor_day's week, Monday first."""
    monday = anchor_day - datetime.timedelta(days=anchor_day.weekday())
    return [monday + datetime.timedelta(days=i) for i in range(7)]


def summarize_week(history, anchor_day, tz=None):
    days = []
    for d in week_days(anchor_day):
        s = summarize_day(history, d, tz)
        if s["snapshots"]:
            days.append(s)
    out = {"anchor": anchor_day, "days": days}
    if days:
        out["listened_seconds"] = sum(d["listened_seconds"] for d in days)
        out["average_seconds"] = out["listened_seconds"] / len(days)
        out["best"] = max(days, key=lambda d: d["listened_seconds"])
        out["queue_start"] = days[0]["queue_start"]
        out["queue_end"] = days[-1]["queue_end"]
        out["behind_delta"] = out["queue_end"]["behind_seconds"] - out["queue_start"]["behind_seconds"]
        out["arrivals_seconds"] = sum(d["arrivals_seconds"] for d in days)
    return out


# --- scheduling ------------------------------------------------------------

def decide_tick(history, now_utc, tz=None, min_gap_minutes=MIN_GAP_MINUTES):
    """What the hourly tick should do right now: "evening", "morning",
    "pulse" (a quiet reading, history only), or None. Uses the Mac's local
    clock at run time, so it follows travel between time zones without
    reloading the launchd agent."""
    now_local = now_utc.astimezone(tz or ps.LOCAL_TZ)
    day = day_of(now_local)
    labels = {s["label"] for s in day_snapshots(history, day, tz)}
    gap = None
    if history:
        gap = now_utc - parse_utc(history[-1]["ts"])
        if gap < datetime.timedelta(minutes=min_gap_minutes):
            return None
    # The evening fallback is checked first so a Mac that was asleep all
    # day still closes the day once it wakes after 10pm.
    if "evening" not in labels and now_local.hour >= EVENING_FALLBACK_HOUR:
        return "evening"
    if (
        "morning" not in labels
        and MORNING_FALLBACK_HOUR <= now_local.hour < MORNING_FALLBACK_END_HOUR
    ):
        return "morning"
    # Quiet readings keep the history dense enough that a Dash announced
    # after the fact (tag-dash) still has readings close to its real times.
    if (
        PULSE_FIRST_HOUR <= now_local.hour < PULSE_LAST_HOUR
        and (gap is None or gap >= datetime.timedelta(minutes=PULSE_EVERY_MINUTES))
    ):
        return "pulse"
    return None


# --- tagging a Dash after the fact -------------------------------------------

TRIGGER_LABELS = ("morning", "evening", "dash-start", "dash-end")


def parse_clock(text):
    """'10:11am', '6:18pm', '18:18', '10am' -> datetime.time"""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([ap]m)?\s*", text.lower())
    if not m:
        raise ValueError(f"can't read the time {text!r}")
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    return datetime.time(hour, minute)


def tag_dash(history, day, start_time, end_time, tz=None, within_minutes=60):
    """Label the readings nearest a Dash's real start and end as dash-start and
    dash-end (marked approximate). Returns (history, messages, ok). Only
    ordinary readings (check-ins and pulses) are relabeled; a reading that is
    already a morning, evening, or Dash reading is never overwritten."""
    zone = tz or ps.LOCAL_TZ
    rows = [dict(r) for r in history]

    def at(clock_time, plus_day=0):
        base = datetime.datetime.combine(day + datetime.timedelta(days=plus_day), clock_time, tzinfo=zone)
        # times before the 4am day boundary belong to the next calendar date
        if clock_time.hour < DAY_STARTS_HOUR:
            base += datetime.timedelta(days=1)
        return base.astimezone(datetime.timezone.utc)

    start_dt, end_dt = at(start_time), at(end_time)
    if end_dt <= start_dt:
        return history, ["the end is not after the start"], False

    def nearest(target, taken):
        best = None
        for i, r in enumerate(rows):
            if i in taken or r["label"] in TRIGGER_LABELS:
                continue
            gap = abs((parse_utc(r["ts"]) - target).total_seconds()) / 60
            if gap <= within_minutes and (best is None or gap < best[0]):
                best = (gap, i)
        return best

    messages, taken = [], set()
    ok = True
    for label, target, shown in (("dash-start", start_dt, start_time), ("dash-end", end_dt, end_time)):
        best = nearest(target, taken)
        if best is None:
            messages.append(f"no ordinary reading within {within_minutes} minutes of "
                            f"{shown.strftime('%-I:%M%p').lower()}; {label} not tagged")
            ok = False
            continue
        gap, i = best
        rows[i]["label"] = label
        rows[i]["approx"] = True
        taken.add(i)
        got = local_dt(rows[i], zone).strftime("%-I:%M%p").lower()
        messages.append(f"{label}: tagged the {got} reading ({gap:.0f} min from "
                        f"{shown.strftime('%-I:%M%p').lower()})")
    if not ok:
        return history, messages, False
    return rows, messages, True


def write_history(rows, path=None):
    with open(path or HISTORY_PATH, "w") as f:
        for r in sorted(rows, key=lambda r: r["ts"]):
            f.write(json.dumps(r, sort_keys=True) + "\n")


# --- formatting ------------------------------------------------------------

def dur(seconds):
    return ps.fmt_duration(abs(seconds))


def dur_n(seconds, units=2):
    """Compact form for table rows: only the largest few units."""
    words = dur(seconds).split()
    pairs = [" ".join(words[i:i + 2]) for i in range(0, len(words), 2)]
    return " ".join(pairs[:units])


def signed(seconds):
    if round(seconds) == 0:
        return "no change"
    return ("+" if seconds > 0 else "-") + dur(seconds)


def clock(snap, tz=None):
    return local_dt(snap, tz).strftime("%-I:%M%p").lower()


def pretty_day_full(day):
    """"Friday, Oct 16th, 2026" """
    return f"{day.strftime('%A, %b')} {rr.ordinal(day.day)}, {day.year}"


def upper_keep_ordinal(text):
    """Uppercase for a heading, but leave the ordinal suffix lowercase: 16th, not 16TH."""
    return re.sub(r"(\d)(ST|ND|RD|TH)\b", lambda m: m.group(1) + m.group(2).lower(), text.upper())


def pretty_day(day):
    return day.strftime("%A, %b %-d")


def behind_phrase(delta):
    if round(delta) == 0:
        return "no change"
    return f"{'improved' if delta < 0 else 'slipped'} {dur(delta)}"


def mood(delta):
    """The tagline: how the day treated the backlog."""
    if round(delta) < 0:
        return ""
    if round(delta) == 0:
        return "and held the line"
    return "and the backlog crept up"


def day_headline(s):
    return " ".join(f"That papa got through {dur(s['listened_seconds'])} of podcasts today {mood(s['behind_delta'])}".split()) + " 🎧"


def week_headline(w):
    return " ".join(f"That papa got through {dur(w['listened_seconds'])} of podcasts this week {mood(w['behind_delta'])}".split()) + " 🎧"


def emoji_row_html():
    return "".join(
        f'<span style="display:inline-block;margin:0 2px;">{e}</span>'
        for e in ps.EMOJI_POOL
    )


def label_name(snap):
    icon, name = LABEL_NAMES.get(snap["label"], ("📸", "Check-in"))
    return f"{icon} {name}" + (" ≈" if snap.get("approx") else "")


def episodes_phrase(n):
    return f"{n} episode" + ("" if n == 1 else "s")


def window_story(w):
    """One reading's window in plain words. Returns (text, tone, delta): tone is
    "better"/"same"/"worse" for the queue and delta its change, or None for the
    day's first reading."""
    sn = w["snap"]
    q = sn["queue_count"]
    if w.get("first"):
        return f"☀️ {episodes_phrase(q)} in the queue", None, None
    played, n = w["played"], sn["played_count"]
    qd, arr = w["queue_delta"], w.get("arrivals_count") or 0
    if played > 0:
        head = f"Finished {episodes_phrase(n)} ({dur_n(played)} of audio)"
    else:
        head = "No episodes finished"
        pr = w.get("progress")
        if pr and abs(pr["delta"]) >= 30:
            span = f" ({pr['from_pct']}% → {pr['to_pct']}%)" if pr["from_pct"] is not None else ""
            head = (f"Kept listening: moved ahead {dur_n(pr['delta'])} in the current episode{span}"
                    if pr["delta"] > 0 else
                    f"Replayed: went back {dur_n(abs(pr['delta']))} in the current episode{span}")
    if qd < 0:
        tail, tone = f"The queue decreased from {q - qd} to {q} episodes", "better"
    elif qd > 0:
        tail, tone = f"The queue increased from {q - qd} to {q} episodes", "worse"
    else:
        tail, tone = f"The queue stayed at {q} episodes", "same"
    return f"{head}. {tail}", tone, qd


def window_sentence(w):
    return window_story(w)[0]


def pace_line(s):
    """At the net pace so far, how long until the queue is clear."""
    if s.get("elapsed_hours", 0) < 3:
        return None
    delta = s["queue_seconds_delta"]
    qe = s["queue_end"]
    so_far = "" if s["closed"] else " so far"
    if delta >= 0:
        return ("📈", f"The queue isn't shrinking yet{so_far}: arrivals are keeping up with your listening")
    per_day = -delta / s["elapsed_hours"] * 24
    days = qe["queue_seconds"] / per_day
    if days > 60:
        return ("🐢", f"At this pace{so_far} it would take over 60 days to clear the queue")
    return ("🏁", f"At this pace{so_far} you'd clear the queue in about "
                  f"{days:.0f} day{'' if round(days) == 1 else 's'}")


def vs_lines(s, tz=None):
    """Plain comparisons of listening with earlier days at the same point in the
    day, so a half-finished day is never held against a whole one."""
    lines = []
    y = s.get("yesterday_listened")
    if y is not None:
        diff = s["listened_seconds"] - y
        if round(diff):
            lines.append(("📈" if diff > 0 else "📉",
                          f"You've finished {dur_n(abs(diff))} {'more' if diff > 0 else 'less'} than yesterday "
                          f"at this time"))
        else:
            lines.append(("📊", "You're right on yesterday's pace"))
    avg, n = s.get("recent_average"), s.get("recent_days", 0)
    if avg is not None and n >= 2:
        diff = s["listened_seconds"] - avg
        lines.append(("📊", f"You're {dur_n(abs(diff))} {'above' if diff >= 0 else 'below'} your usual "
                            f"at this time"))
    return lines


def highlights(s, tz=None):
    """(icon, sentence) pairs worth a glance."""
    out = []
    b = s.get("biggest_listen")
    if b:
        start = local_dt(b["snap"], tz, key="since").strftime("%-I:%M%p").lower()
        n = b["snap"]["played_count"]
        out.append(("🏅", f"Biggest stretch: {episodes_phrase(n)} finished ({dur_n(b['played'])} of audio), "
                          f"{start} to {clock(b['snap'], tz)}"))
    a = s.get("biggest_arrival")
    if a and a["arrivals"] >= 1800:
        start = local_dt(a["snap"], tz, key="since").strftime("%-I:%M%p").lower()
        n = a["arrivals_count"]
        out.append(("📬", f"Biggest arrival burst: {n} episode{'' if n == 1 else 's'} "
                          f"({dur_n(a['arrivals'])}), {start} to {clock(a['snap'], tz)}"))
    parts = s.get("by_part") or []
    if parts and s["listened_seconds"] >= 3600:
        top = parts[0]
        pct = 100.0 * top["seconds"] / s["listened_seconds"]
        if pct >= 40:
            out.append((top["icon"], f"Your peak was {top['name']} ({top['range']}): {dur_n(top['seconds'])} of "
                                     f"the {dur_n(s['listened_seconds'])} you finished ({pct:.0f}%)"))
    pace = pace_line(s)
    if pace:
        out.append(pace)
    if s["drought"] and s["drought"][0] >= 3 * 3600:
        span, x, y = s["drought"]
        mid_dash = any(d["start"]["ts"] < y and d["end"]["ts"] > x for d in s["dashes"])
        out.append(("🤫" if mid_dash else "😴", f"{'Quiet stretch mid-Dash' if mid_dash else 'Nap time'}: {dur_n(span)} with nothing played, "
                          f"{local_dt({'ts': x}, tz).strftime('%-I:%M%p').lower()} to "
                          f"{local_dt({'ts': y}, tz).strftime('%-I:%M%p').lower()}"))
    for icon, line in vs_lines(s, tz):
        out.append((icon, line))
    return out


def waiting_age(oldest_iso, now_utc):
    """How long a show's oldest unplayed episode has been waiting."""
    if not oldest_iso:
        return None
    return (now_utc - parse_utc(oldest_iso)).total_seconds()


def pile_rows(s, d=None, now=None):
    """Every show with something waiting, stalest first. Uses the latest run's
    full queue when there is one, else the latest reading that recorded it."""
    now = now or utcnow()
    if d:
        rows = queue_by_show(d, episodes=True)
    else:
        snap = s.get("by_show_snapshot")
        rows = sort_oldest_first(list(snap.get("by_show") or [])) if snap else []
    return [dict(r, age=waiting_age(r.get("oldest"), now)) for r in rows]


def ago_phrase(seconds):
    if seconds is None:
        return ""
    if seconds < 3600:
        return "just now"
    if seconds < 86400:
        n = int(seconds // 3600)
        return f"{n} hr{'' if n == 1 else 's'} ago"
    n = int(seconds // 86400)
    return f"{n} day{'' if n == 1 else 's'} ago"


def published_label(oldest_iso, tz=None):
    """"Tue, Oct 6" in local time."""
    if not oldest_iso:
        return ""
    d = parse_utc(oldest_iso).astimezone(tz or ps.LOCAL_TZ)
    return f"{d.strftime('%a, %b')} {d.day}"


def pile_line(row, tz=None):
    """One line for chat and Discord: the count, the listening time, and the
    oldest episode with its date."""
    bits = [episodes_phrase(row["count"]), dur_n(row["seconds"])]
    if row.get("oldest_title"):
        bits.append(f"oldest: {row['oldest_title']} ({published_label(row.get('oldest'), tz)})")
    elif row.get("oldest"):
        bits.append(f"oldest published {published_label(row['oldest'], tz)}")
    return " · ".join(bits)


def bar_chat(value, peak, width=10):
    if peak <= 0 or value <= 0:
        return "·" * 1
    return "█" * max(1, round(width * value / peak))


def render_day_chat(s, tz=None):
    if not s["snapshots"]:
        return f"🎧 Daily Flow — {pretty_day(s['day'])}\nNo snapshots recorded."
    qs, qe = s["queue_start"], s["queue_end"]
    lines = [
        ps.emoji_header(),
        day_headline(s),
        f"🎧 Daily Flow — {pretty_day(s['day'])}",
        f"Finished: {dur(s['listened_seconds'])} of audio · {s['listened_count']} episodes",
        f"Queue: {qs['queue_count']} → {qe['queue_count']} episodes ({signed(s['queue_seconds_delta'])})",
        f"Behind: {dur(qs['behind_seconds'])} → {dur(qe['behind_seconds'])} ({behind_phrase(s['behind_delta'])})",
        f"New arrivals: {dur(s['arrivals_seconds'])}",
    ]
    for icon, line in highlights(s, tz):
        lines.append(f"{icon} {line}")
    for dsh in s["dashes"]:
        lines.append(
            f"🚐 Dash {clock(dsh['start'], tz)}–{clock(dsh['end'], tz)}: "
            f"finished {dur(dsh['listened_seconds'])} of audio, queue {signed(dsh['queue_seconds_delta'])}, "
            f"behind {behind_phrase(dsh['behind_delta'])}"
        )
    rows = pile_rows(s)
    if rows:
        lines.append("")
        lines.append(f"What's piling up ({len(rows)} shows, oldest first):")
        for row in rows[:8]:
            lines.append(f"  {row['podcast']}: {pile_line(row)}")
        if len(rows) > 8:
            lines.append(f"  ... and {len(rows) - 8} more")
    if s.get("now_playing"):
        np = s["now_playing"]
        lines.append(f"▶ Now playing: {np['title']} — {np['podcast']}")
    lines.append("")
    peak = max([w["played"] for w in s["windows"]] + [1])
    for w in s["windows"]:
        sn = w["snap"]
        lines.append(f"{clock(sn, tz):>8}  {label_name(sn):<14} {bar_chat(w['played'], peak):<10}  {window_sentence(w)}")
    if any(w["snap"].get("approx") for w in s["windows"]):
        lines.append("≈ labeled from the nearest scheduled reading")
    return "\n".join(lines)


def render_week_chat(w, tz=None):
    if not w["days"]:
        return "🎧 Weekly Flow\nNo snapshots recorded this week."
    first, last = w["days"][0]["day"], w["days"][-1]["day"]
    best = w["best"]
    lines = [
        ps.emoji_header(),
        week_headline(w),
        f"🎧 Weekly Flow — {first.strftime('%b %-d')} to {last.strftime('%b %-d')}",
        f"Finished: {dur(w['listened_seconds'])} of audio across {len(w['days'])} days, "
        f"{dur(w['average_seconds'])} a day on average",
        f"Best day: {pretty_day(best['day'])}, {dur(best['listened_seconds'])}",
        f"Queue: {w['queue_start']['queue_count']} → {w['queue_end']['queue_count']} episodes, "
        f"behind {behind_phrase(w['behind_delta'])}",
        f"New arrivals: {dur(w['arrivals_seconds'])}",
        "",
    ]
    for d in w["days"]:
        lines.append(
            f"{d['day'].strftime('%a %b %-d'):<10} finished {dur(d['listened_seconds']):<28} "
            f"queue {d['queue_end']['queue_count']} eps"
        )
    return "\n".join(lines)


CARD_VALUE_SIZE = 20


def fit_value(text):
    """A card's big number: one size on every card, never wrapping."""
    return (f'<span style="font-size:{CARD_VALUE_SIZE}px;white-space:nowrap;">{html.escape(text)}</span>')


def _card(label, value, sub="", hint="", value_html=None, sub_html=None):
    hint_html = (f'<div style="font-size:11px;color:#94a3b8;margin-top:8px;">{html.escape(hint)}</div>'
                 if hint else "")
    if sub_html is not None:
        sub_html = f'<div style="font-size:13px;margin-top:6px;">{sub_html}</div>'
    else:
        sub_html = (f'<div style="font-size:13px;color:#64748b;margin-top:6px;">{html.escape(sub)}</div>'
                    if sub else "")
    value_part = value_html if value_html is not None else fit_value(value)
    return (
        '<td width="50%" valign="top" style="padding:0 8px 16px 0;">'
        '<div style="background:#ffffff;border-radius:12px;padding:20px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.08);">'
        f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#94a3b8;'
        f'letter-spacing:0.6px;margin-bottom:8px;">{html.escape(label)}</div>'
        f'<div style="font-size:22px;font-weight:700;line-height:1.2;">{value_part}</div>'
        f'{sub_html}{hint_html}'
        '</div></td>'
    )


def keycaps(n):
    """121 -> 1️⃣2️⃣1️⃣"""
    return "".join(f"{ch}\ufe0f\u20e3" for ch in str(int(n)))


QUEUE_TONES = {
    "better": ("#dcfce7", "#15803d", "\u25bc"),
    "same": ("#fef9c3", "#a16207", "\u25ac"),
    "worse": ("#fee2e2", "#b91c1c", "\u25b2"),
}


def queue_tone(start_count, end_count):
    return "better" if end_count < start_count else ("worse" if end_count > start_count else "same")


def since_label(snap, tz=None):
    """"since 6:06am EDT" - the one starting point every card on the page uses."""
    return f"since {clock(snap, tz)} {local_dt(snap, tz).strftime('%Z')}"


def arrivals_tone(arrived, listened):
    """Blue when you listened to more than arrived, yellow within 10% of it,
    red when more arrived than you listened to."""
    if arrived > listened * 1.1:
        return "worse"
    if arrived < listened * 0.9:
        return "better"
    return "same"


ARRIVAL_COLORS = {"better": "#1d4ed8", "same": QUEUE_TONES["same"][1], "worse": QUEUE_TONES["worse"][1]}


def arrivals_line(count, arrived_seconds, listened_seconds, since):
    """Blue when you listened to more than arrived (arrivals are not 'good',
    so no green), yellow when about even, red when more arrived."""
    tone = arrivals_tone(arrived_seconds, listened_seconds)
    fg = ARRIVAL_COLORS[tone]
    noun = f"{count} episode" + ("" if count == 1 else "s")
    return f'<span style="color:{fg};font-weight:600;">\u25b2 {html.escape(noun)} {html.escape(since)}</span>'


def age_line(delta, since):
    """The oldest-episode change, colored like the queue card: green when the
    oldest unplayed episode is newer than at the start, yellow when unchanged,
    red when it has slipped further behind."""
    tone = "better" if round(delta) < 0 else ("worse" if round(delta) > 0 else "same")
    _, fg, arrow = QUEUE_TONES[tone]
    text = {"better": f"{arrow} Improved {dur_n(delta)} {since}",
            "same": f"{arrow} No change {since}",
            "worse": f"{arrow} Slipped {dur_n(delta)} {since}"}[tone]
    return f'<span style="color:{fg};font-weight:600;">{html.escape(text)}</span>'


def queue_card(qs, qe, tz=None):
    """The queue as its count in big keycap digits on green (shorter than when
    the day started), yellow (the same), or red (longer)."""
    tone = queue_tone(qs["queue_count"], qe["queue_count"])
    bg, fg, arrow = QUEUE_TONES[tone]
    delta = qe["queue_count"] - qs["queue_count"]
    since = since_label(qs, tz)
    n = abs(delta)
    noun = f"{n} episode" + ("" if n == 1 else "s")
    sub = {"better": f"{arrow} {noun} {since}",
           "same": f"{arrow} No change {since}",
           "worse": f"{arrow} {noun} {since}"}[tone]
    value_html = (f'<span style="display:inline-block;background:{bg};border-radius:12px;'
                  f'padding:6px 14px;font-size:30px;line-height:1.15;letter-spacing:2px;">'
                  f'{keycaps(qe["queue_count"])}</span>')
    sub_html = f'<span style="color:{fg};font-weight:600;">{html.escape(sub)}</span>'
    return (
        '<td width="50%" valign="top" style="padding:0 8px 16px 0;">'
        '<div style="background:#ffffff;border-radius:12px;padding:20px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">'
        '<div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#94a3b8;'
        'letter-spacing:0.6px;margin-bottom:8px;">Episodes in your queue</div>'
        f'<div>{value_html}</div>'
        f'<div style="font-size:13px;margin-top:10px;">{sub_html}</div>'
        '</div></td>'
    )


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def long_stamp(generated, tz=None):
    """"Friday, October 16th, 2026 @ 4:18pm EDT" - same form as the main report."""
    local = generated.astimezone(tz or ps.LOCAL_TZ)
    return (
        local.strftime(f"%A, %B {rr.ordinal(local.day)}, %Y @ ")
        + local.strftime("%-I:%M%p").lower() + local.strftime(" %Z")
    )


def generated_pill(generated, tz=None):
    """The same pill the main report uses, stamped with when this was built."""
    return (
        '<span style="display:inline-block;background:#e2e8f0;color:#64748b;font-size:11px;'
        'font-weight:600;letter-spacing:0.3px;border-radius:20px;padding:5px 14px;">'
        f'\U0001f552 Generated {html.escape(long_stamp(generated, tz))}</span>'
    )


def queue_section_html(d):
    """The whole unplayed queue, every episode with its artwork, for appending
    under the flow summary (the web page and the optional email)."""
    return (
        '<div style="border-top:2px dashed #cbd5e1;margin:40px 0 28px;"></div>'
        '<div style="text-align:center;font-size:13px;font-weight:600;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.6px;margin-bottom:20px;">The unplayed queue</div>'
        + rr.build_html(d, embed=True)
    )


def _page(title, body, headline=None, generated=None, tz=None, queue_html=""):
    generated = generated or utcnow()
    headline_html = (
        f'<div style="text-align:center;font-size:20px;font-weight:600;margin-bottom:8px;">'
        f'{html.escape(headline)}</div>' if headline else ""
    )
    pill = generated_pill(generated, tz)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(title)} · Podcast Queue Report</title></head>
<body style="margin:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
<div style="max-width:720px;margin:0 auto;padding:32px 20px;">
<div style="text-align:center;font-size:25px;line-height:1.3;margin-bottom:16px;white-space:nowrap;">{emoji_row_html()}</div>
{headline_html}
<div style="text-align:center;font-size:13px;font-weight:600;color:#64748b;letter-spacing:0.6px;">{html.escape(upper_keep_ordinal(title))}</div>
<div style="text-align:center;margin:10px 0 4px;">{pill}</div>
{body}
{queue_html}
<div style="text-align:center;margin-top:32px;">{pill}</div>
</div></body></html>"""


def _white_card(inner, margin=16, extra=""):
    return (f'<div style="background:#ffffff;border-radius:12px;padding:16px 20px;margin-bottom:{margin}px;'
            f'box-shadow:0 1px 3px rgba(0,0,0,0.08);{extra}">{inner}</div>')


def window_label(snap, tz=None):
    """"2:06\u20134:06pm", or "10:06am\u201312:06pm" when the window crosses noon."""
    start = local_dt(snap, tz, key="since")
    end = local_dt(snap, tz)
    s_ap, e_ap = start.strftime("%p").lower(), end.strftime("%p").lower()
    s_txt = start.strftime("%-I:%M")
    return (f"{s_txt}\u2013{end.strftime('%-I:%M')}{e_ap}" if s_ap == e_ap
            else f"{s_txt}{s_ap}\u2013{end.strftime('%-I:%M')}{e_ap}")


def mini_playing_html(np):
    """What was playing at a reading: small artwork, linked title and show."""
    if not np or not np.get("title"):
        return ""
    art = ""
    if np.get("artwork_url"):
        img = (f'<img src="{html.escape(np["artwork_url"])}" width="36" height="36" '
               'style="border-radius:6px;display:block;" alt="">')
        link = np.get("episode_url") or np.get("podcast_url")
        art = f'<a href="{html.escape(link)}" target="_blank">{img}</a>' if link else img
    return (
        '<div style="display:flex;gap:8px;align-items:center;margin-top:6px;">'
        + (f'<div style="flex:0 0 36px;">{art}</div>' if art else "")
        + '<div style="flex:1;min-width:0;font-size:12px;line-height:1.35;">'
        '<span style="color:#047857;font-weight:700;">▶ Playing</span> '
        f'{_link(np["title"], np.get("episode_url"), "#065f46", 600)}'
        f'<div style="color:#64748b;">{_link(np.get("podcast"), np.get("podcast_url"), "#64748b")}</div></div></div>'
    )


def timeline_html(s, tz=None):
    peak = max([w["played"] for w in s["windows"]] + [1])
    in_dash = False
    rows = ""
    last_playing = None
    richest = {}
    for w in s["windows"]:
        np = w["snap"].get("now_playing")
        if np and np.get("title"):
            merged = richest.setdefault(np["title"], {})
            for k, v in np.items():
                if v is not None and k not in merged:
                    merged[k] = v
    for w in s["windows"]:
        sn = w["snap"]
        shaded = in_dash or sn["label"] == "dash-end"
        dash_style = "background:#f0f9ff;border-left:4px solid #38bdf8;" if shaded else "border-left:4px solid transparent;"
        width = max(3, round(100 * w["played"] / peak)) if w["played"] > 0 else 0
        bar = (f'<div style="height:10px;border-radius:5px;background:{"#38bdf8" if shaded else "#34d399"};width:{width}%;"></div>'
               if width else '<div style="font-size:11px;color:#cbd5e1;">·</div>')
        text, tone, delta = window_story(w)
        chip_css = ("flex:0 0 38px;text-align:center;box-sizing:border-box;font-size:12px;font-weight:700;"
                    "border-radius:10px;padding:1px 0;white-space:nowrap;")
        if tone:
            bg, fg, arrow = QUEUE_TONES[tone]
            chip = (f'<span style="{chip_css}background:{bg};color:{fg};">'
                    f'{arrow}{"" if not delta else " " + str(abs(delta))}</span>')
        else:
            chip = f'<span style="{chip_css}"></span>'
        when = clock(sn, tz) if w.get("first") else window_label(sn, tz)
        # show what was playing only when it changes, not on every row of one long episode
        np = sn.get("now_playing")
        playing_html = (mini_playing_html(dict(richest.get(np["title"], {}), **np))
                        if np and np.get("title") != last_playing else "")
        if np:
            last_playing = np.get("title")
        rows += (
            f'<tr style="{dash_style}">'
            f'<td style="padding:9px 12px;white-space:nowrap;vertical-align:top;">'
            f'<div style="font-weight:700;font-size:14px;">{html.escape(when)}</div>'
            f'<div style="font-size:12px;color:#64748b;">{html.escape(label_name(sn))}</div></td>'
            f'<td style="padding:9px 12px;width:22%;vertical-align:middle;">{bar}</td>'
            f'<td style="padding:9px 12px 9px 4px;font-size:13px;vertical-align:top;line-height:1.35;">'
            f'<div style="display:flex;gap:8px;align-items:flex-start;">{chip}'
            f'<div style="flex:1;min-width:0;">{html.escape(text)}{playing_html}</div></div></td>'
            '</tr>'
        )
        if sn["label"] == "dash-start":
            in_dash = True  # the rows after the start are the Dash itself
        if sn["label"] == "dash-end":
            in_dash = False
            for dsh in s["dashes"]:
                if dsh["end"] is sn:
                    qd, bd = dsh["queue_seconds_delta"], dsh["behind_delta"]
                    queue_bit = ("the queue got " + (f"{dur_n(abs(qd))} shorter" if qd < 0 else f"{dur_n(qd)} longer")
                                 if qd else "the queue didn't move")
                    age_bit = (f"improved by {dur_n(bd)}" if bd < 0
                               else f"slipped by {dur_n(bd)}" if bd > 0 else "stayed the same")
                    rows += (
                        '<tr style="background:#e0f2fe;border-left:4px solid #38bdf8;">'
                        f'<td colspan="3" style="padding:10px 12px;font-size:13px;font-weight:600;color:#0369a1;">'
                        f'🚐 Dash {clock(dsh["start"], tz)}\u2013{clock(dsh["end"], tz)}: you finished '
                        f'{html.escape(episodes_phrase(dsh["listened_count"]))} ({html.escape(dur_n(dsh["listened_seconds"]))} of audio), '
                        f'{html.escape(queue_bit)}, and your oldest episode {html.escape(age_bit)}'
                        '</td></tr>'
                    )
    note = ('<div style="font-size:11px;color:#94a3b8;margin-top:8px;">≈ labeled from the nearest scheduled reading</div>'
            if any(w["snap"].get("approx") for w in s["windows"]) else "")
    return (
        '<div style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;'
        'margin:8px 0 10px;">The shape of your day</div>'
        '<div style="background:#ffffff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.08);overflow:hidden;">'
        f'<table width="100%" cellpadding="0" cellspacing="0">{rows}</table></div>{note}'
    )


def piling_up_html(s, d=None, tz=None):
    rows = pile_rows(s, d)
    if not rows:
        return ""
    items = ""
    now_utc = utcnow()
    for row in rows:
        art = ""
        if row.get("artwork_url"):
            img = (f'<img src="{html.escape(row["artwork_url"])}" width="40" height="40" '
                   'style="border-radius:7px;display:block;" alt="">')
            art = (f'<a href="{html.escape(row["podcast_url"])}" target="_blank">{img}</a>'
                   if row.get("podcast_url") else img)
        bullets = ""
        if row.get("episodes"):
            for ep in row["episodes"]:
                age = (now_utc - parse_utc(ep["pubdate"])).total_seconds() if ep.get("pubdate") else None
                when = (f'{published_label(ep["pubdate"], tz)} · {ago_phrase(age)}' if ep.get("pubdate") else "")
                bullets += (
                    '<div style="font-size:12px;margin-top:4px;color:#475569;">'
                    f'• {_link(ep.get("title") or "Untitled episode", ep.get("url"), "#1e293b")}'
                    f'<span style="color:#94a3b8;"> · {html.escape(when)}</span></div>'
                )
        elif row.get("oldest_title") or row.get("oldest"):
            when = (f'{published_label(row["oldest"], tz)} · {ago_phrase(row.get("age"))}' if row.get("oldest") else "")
            title = (_link(row["oldest_title"], row.get("oldest_url"), "#1e293b") if row.get("oldest_title")
                     else "oldest episode")
            bullets = (f'<div style="font-size:12px;margin-top:4px;color:#475569;">• {title}'
                       f'<span style="color:#94a3b8;"> · {html.escape(when)}</span></div>')
        items += (
            '<div style="display:flex;gap:12px;align-items:flex-start;padding:10px 0;border-bottom:1px solid #f1f5f9;">'
            f'<div style="flex:0 0 40px;">{art}</div>'
            '<div style="flex:1;min-width:0;">'
            f'<div style="font-size:13px;font-weight:700;">{_link(row["podcast"], row.get("podcast_url"), "#1e293b")}</div>'
            f'{bullets}</div>'
            '<div style="text-align:right;font-size:12px;color:#64748b;white-space:nowrap;">'
            f'{html.escape(episodes_phrase(row["count"]))}<br>{html.escape(dur_n(row["seconds"]))}</div></div>'
        )
    total = sum(r["count"] for r in rows)
    return (
        '<div style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;'
        'margin:24px 0 4px;">What&rsquo;s piling up</div>'
        f'<div style="font-size:12px;color:#94a3b8;margin-bottom:10px;">{len(rows)} shows, {total} episodes</div>'
        + _white_card(items, 16)
    )


def _link(text, url, color="inherit", weight=None):
    inner = html.escape(text or "")
    if not url:
        return inner
    style = f"color:{color};text-decoration:none;" + (f"font-weight:{weight};" if weight else "")
    return f'<a href="{html.escape(url)}" target="_blank" style="{style}">{inner}</a>'


def now_playing_card_html(np, up_next=None):
    """The episode playing right now: artwork, linked title and show, how far
    through it you are, and what is next."""
    if not np:
        return ""
    art = (f'<img src="{html.escape(np["artwork_url"])}" width="64" height="64" '
           'style="border-radius:10px;display:block;" alt="">') if np.get("artwork_url") else ""
    if np.get("artwork_url") and (np.get("episode_url") or np.get("podcast_url")):
        art = f'<a href="{html.escape(np.get("episode_url") or np.get("podcast_url"))}" target="_blank">{art}</a>'
    progress = ""
    dur_s, el_s = np.get("duration_seconds") or 0, np.get("elapsed_seconds") or 0
    if dur_s > 0:
        pct = max(0, min(100, 100 * el_s / dur_s))
        left = np.get("remaining_fmt") or ""
        progress = (
            '<div style="height:6px;border-radius:3px;background:#bbf7d0;margin-top:10px;">'
            f'<div style="height:6px;border-radius:3px;background:#16a34a;width:{pct:.0f}%;"></div></div>'
            f'<div style="font-size:12px;color:#047857;margin-top:5px;">{pct:.0f}% played'
            + (f" · {html.escape(left)} left" if left else "") + '</div>'
        )
    nxt = ""
    if up_next and up_next.get("title"):
        small = ""
        if up_next.get("artwork_url"):
            img = (f'<img src="{html.escape(up_next["artwork_url"])}" width="40" height="40" '
                   'style="border-radius:7px;display:block;" alt="">')
            link = up_next.get("episode_url") or up_next.get("podcast_url")
            small = f'<a href="{html.escape(link)}" target="_blank">{img}</a>' if link else img
        nxt = (
            '<div style="display:flex;gap:12px;align-items:center;margin-top:12px;padding-top:12px;'
            'border-top:1px solid #bbf7d0;">'
            f'<div style="flex:0 0 40px;">{small}</div>'
            '<div style="flex:1;min-width:0;font-size:12px;color:#475569;">'
            '<span style="display:inline-block;background:#dbeafe;color:#1d4ed8;font-size:10px;font-weight:700;'
            'letter-spacing:0.5px;border-radius:4px;padding:1px 6px;margin-bottom:3px;">▶ UP NEXT</span>'
            f'<div style="font-size:13px;font-weight:600;">{_link(up_next["title"], up_next.get("episode_url"), "#1e293b")}</div>'
            f'<div>{_link(up_next.get("podcast"), up_next.get("podcast_url"), "#64748b")}'
            + (f' · {html.escape(up_next["duration_fmt"])}' if up_next.get("duration_fmt") else "")
            + '</div></div></div>'
        )
    return (
        '<div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:12px;padding:16px 20px;margin-bottom:20px;">'
        '<div style="display:flex;gap:16px;align-items:flex-start;">'
        f'<div style="flex:0 0 64px;">{art}</div>'
        '<div style="flex:1;min-width:0;">'
        f'{rr.now_playing_badge()}'
        f'<div style="font-size:15px;font-weight:700;margin-top:6px;color:#065f46;">'
        f'{_link(np.get("title"), np.get("episode_url"), "#065f46")}</div>'
        f'<div style="font-size:13px;margin-top:2px;color:#047857;">{_link(np.get("podcast"), np.get("podcast_url"), "#047857")}</div>'
        f'{progress}</div></div>{nxt}</div>'
    )


def queue_chart_svg(s, tz=None, width=680, height=190):
    """The queue's size at each reading across the day: a line with a tinted
    area, a dot per reading, the Dash shaded, the start and the latest labeled."""
    snaps = s["snapshots"]
    if len(snaps) < 2:
        return ""
    zone = tz or ps.LOCAL_TZ
    left, right, top, bottom = 38, 14, 16, 30
    w, h = width - left - right, height - top - bottom
    times = [parse_utc(x["ts"]).timestamp() for x in snaps]
    t0, t1 = times[0], times[-1]
    span = (t1 - t0) or 1
    counts = [x["queue_count"] for x in snaps]
    vmax, vmin = max(counts) + 1, min(counts) - 1

    def X(ts):
        return left + (ts - t0) / span * w

    def Y(v):
        return top + (vmax - v) / ((vmax - vmin) or 1) * h

    tone = queue_tone(counts[0], counts[-1])
    area_bg, line, _ = QUEUE_TONES[tone]
    line = {"better": "#16a34a", "same": "#ca8a04", "worse": "#dc2626"}[tone]
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" '
             f'aria-label="The queue over the day" style="display:block;font-family:inherit;">']
    for v in sorted({vmax, vmin, round((vmax + vmin) / 2)}):
        parts.append(f'<line x1="{left}" y1="{Y(v):.1f}" x2="{left + w}" y2="{Y(v):.1f}" stroke="#e2e8f0" stroke-width="1"/>'
                     f'<text x="{left - 6}" y="{Y(v) + 4:.1f}" font-size="11" fill="#94a3b8" text-anchor="end">{v}</text>')
    # Dash windows
    for dsh in s["dashes"]:
        x1, x2 = X(parse_utc(dsh["start"]["ts"]).timestamp()), X(parse_utc(dsh["end"]["ts"]).timestamp())
        parts.append(f'<rect x="{x1:.1f}" y="{top}" width="{max(x2 - x1, 2):.1f}" height="{h}" fill="#e0f2fe" opacity="0.9"/>'
                     f'<text x="{(x1 + x2) / 2:.1f}" y="{top + 12}" font-size="11" fill="#0369a1" text-anchor="middle">🚐 Dash</text>')
    pts = [(X(ts), Y(c)) for ts, c in zip(times, counts)]
    path = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    base = top + h
    parts.append(f'<polygon points="{pts[0][0]:.1f},{base} {path} {pts[-1][0]:.1f},{base}" fill="{area_bg}" opacity="0.85"/>')
    parts.append(f'<polyline points="{path}" fill="none" stroke="{line}" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>')
    last_dot_x = last_label_x = None
    for i, ((x, y), c) in enumerate(zip(pts, counts)):
        if last_dot_x is None or x - last_dot_x >= 7 or i == len(pts) - 1:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="#fff" stroke="{line}" stroke-width="2"/>')
            last_dot_x = x
        important = i == 0 or c in (min(counts), max(counts))
        if (important or i == len(pts) - 1 or len(pts) <= 12) and (last_label_x is None or x - last_label_x >= 30):
            parts.append(f'<text x="{x:.1f}" y="{y - 9:.1f}" font-size="11" font-weight="700" fill="#334155" '
                         f'text-anchor="middle">{c}</text>')
            last_label_x = x
    # time axis: an hourly tick when the day is long enough, never two labels on top of each other
    first_local = datetime.datetime.fromtimestamp(t0, zone)
    last_local = datetime.datetime.fromtimestamp(t1, zone)
    hour = first_local.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    step = 1 if span <= 8 * 3600 else 2 if span <= 16 * 3600 else 3
    marks = [first_local]
    while hour.timestamp() < t1:
        marks.append(hour)
        hour += datetime.timedelta(hours=step)
    marks.append(last_local)
    kept = []
    for tk in marks:
        x = X(tk.timestamp())
        if kept and x - kept[-1][0] < 44:
            if tk is last_local and len(kept) > 1:
                kept.pop()          # the latest time wins over a tick right beside it
            else:
                continue
        kept.append((x, tk))
    for x, tk in kept:
        label = tk.strftime("%-I%p").lower() if tk.minute == 0 else tk.strftime("%-I:%M%p").lower()
        parts.append(f'<text x="{x:.1f}" y="{height - 8}" font-size="11" fill="#94a3b8" text-anchor="middle">{label}</text>')
    parts.append("</svg>")
    delta = counts[-1] - counts[0]
    sub = (f"{counts[-1]} episodes now, down {abs(delta)} from {counts[0]}" if delta < 0 else
           f"{counts[-1]} episodes now, up {delta} from {counts[0]}" if delta > 0 else
           f"{counts[-1]} episodes now, the same as at the start")
    sub_color = {"better": "#15803d", "same": "#a16207", "worse": "#b91c1c"}[tone]
    return (
        '<div style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;'
        'margin:8px 0 4px;">The queue over your day</div>'
        f'<div style="font-size:12px;font-weight:600;color:{sub_color};margin-bottom:10px;">{html.escape(sub)}</div>'
        + _white_card("".join(parts), 20, "padding:12px 14px;")
    )


def in_progress_html(items):
    """The episodes you have started: artwork, a linked title and show, and how
    far through each one is."""
    if not items:
        return ""
    rows = ""
    for it in items:
        art = ""
        if it.get("artwork_url"):
            img = (f'<img src="{html.escape(it["artwork_url"])}" width="40" height="40" '
                   'style="border-radius:7px;display:block;" alt="">')
            link = it.get("episode_url") or it.get("podcast_url")
            art = f'<a href="{html.escape(link)}" target="_blank">{img}</a>' if link else img
        rows += (
            '<div style="display:flex;gap:12px;align-items:center;padding:9px 0;border-bottom:1px solid #f1f5f9;">'
            f'<div style="flex:0 0 40px;">{art}</div>'
            '<div style="flex:1;min-width:0;">'
            f'<div style="font-size:13px;font-weight:700;">{_link(it.get("title"), it.get("episode_url"), "#1e293b")}</div>'
            f'<div style="font-size:12px;color:#64748b;">{_link(it.get("podcast"), it.get("podcast_url"), "#64748b")}</div>'
            '<div style="height:8px;border-radius:4px;background:#e2e8f0;margin-top:7px;">'
            f'<div style="height:8px;border-radius:4px;background:linear-gradient(90deg,#34d399,#16a34a);width:{it["pct"]}%;"></div></div></div>'
            f'<div style="flex:0 0 auto;text-align:right;font-size:12px;color:#64748b;white-space:nowrap;">'
            f'<div style="font-size:16px;font-weight:800;color:#15803d;">{it["pct"]}%</div>{html.escape(str(it.get("left") or ""))} left</div></div>'
        )
    return (
        '<div style="font-size:13px;font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;'
        'margin:8px 0 10px;">Episodes in progress</div>'
        + _white_card(rows, 20)
    )


def render_day_html(s, tz=None, generated=None, d=None):
    title = f"Daily Flow — {pretty_day_full(s['day'])}"
    if not s["snapshots"]:
        return _page(title, "<p>No snapshots recorded.</p>", None, generated, tz)
    qs, qe = s["queue_start"], s["queue_end"]
    cards = [
        _card("Finished", dur(s["listened_seconds"]),
              f"{s['listened_count']} episode{'' if s['listened_count'] == 1 else 's'} finished {since_label(qs, tz)}"),
        queue_card(qs, qe, tz),
        _card("The oldest unplayed episode in your queue", dur_n(qe['behind_seconds'], 4),
              sub_html=age_line(s["behind_delta"], since_label(qs, tz))),
        _card("New arrivals", dur(s["arrivals_seconds"]),
              sub_html=arrivals_line(s["arrivals_count"], s["arrivals_seconds"], s["listened_seconds"],
                                     since_label(qs, tz))),
    ]
    rows = "".join(f"<tr>{cards[i]}{cards[i + 1]}</tr>" for i in range(0, len(cards), 2))
    parts = [f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;">{rows}</table>']
    hl = highlights(s, tz)
    if hl:
        items = "".join(f'<div style="margin:6px 0;font-size:14px;">{icon} {html.escape(line)}</div>' for icon, line in hl)
        parts.append(_white_card(items, 20))
    np, nxt = s.get("now_playing"), s.get("up_next")
    if d and d.get("now_playing"):
        np = d["now_playing"]
        nxt = rr._next_up_episode(d) if d["queue"].get("episodes") else None
    parts.append(queue_chart_svg(s, tz))
    parts.append(now_playing_card_html(np, nxt))
    started = in_progress_episodes(d) if d else ((s.get("in_progress_snapshot") or {}).get("in_progress") or [])
    parts.append(in_progress_html(started))
    parts.append(timeline_html(s, tz))
    parts.append(piling_up_html(s, d, tz))
    return _page(title, "".join(parts), day_headline(s), generated, tz,
                 queue_section_html(d) if d else "")


def render_week_html(w, tz=None, generated=None, d=None):
    title = "Weekly Flow"
    if not w["days"]:
        return _page(title, "<p>No snapshots recorded this week.</p>", None, generated, tz)
    best = w["best"]
    cards = [
        _card("Finished", dur(w["listened_seconds"]),
              f"{dur(w['average_seconds'])} a day over {len(w['days'])} days"),
        _card("Best day", dur(best["listened_seconds"]), pretty_day(best["day"])),
        _card("Queue", f"{w['queue_start']['queue_count']} → {w['queue_end']['queue_count']} episodes",
              f"behind {behind_phrase(w['behind_delta'])}"),
        _card("New arrivals", dur(w["arrivals_seconds"])),
    ]
    rows = "".join(
        f"<tr>{cards[i]}{cards[i + 1]}</tr>" for i in range(0, len(cards), 2)
    )
    day_rows = "".join(
        f"<tr><td style='padding:8px 12px;'>{d['day'].strftime('%a %b %-d')}</td>"
        f"<td style='padding:8px 12px;'>{html.escape(dur(d['listened_seconds']))}</td>"
        f"<td style='padding:8px 12px;'>{d['queue_end']['queue_count']} eps</td></tr>"
        for d in w["days"]
    )
    body = (
        f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;">{rows}</table>'
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.08);font-size:13px;">'
        "<tr style='text-align:left;color:#94a3b8;font-size:11px;text-transform:uppercase;'>"
        "<th style='padding:12px;'>Day</th><th style='padding:12px;'>Finished</th>"
        f"<th style='padding:12px;'>Queue</th></tr>{day_rows}</table>"
    )
    return _page(title, body, week_headline(w), generated, tz,
                 queue_section_html(d) if d else "")


def _flow_embed(title_line, headline, fields, generated, color, tz=None):
    return {"embeds": [{
        "title": ps.emoji_header(),
        "description": f"**{headline}**\n{title_line}\n\U0001f552 Generated {long_stamp(generated, tz)}",
        "color": color,
        "fields": fields,
        "footer": {"text": "Podcast Queue Report \u00b7 Daily Flow"},
        "timestamp": generated.astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
    }]}


def _field(name, value, inline=True):
    return {"name": name, "value": value or "-", "inline": inline}


def build_discord_day(s, generated=None, tz=None):
    generated = generated or utcnow()
    if not s["snapshots"]:
        return {"content": f"Daily Flow, {pretty_day(s['day'])}: no snapshots recorded."}
    qs, qe = s["queue_start"], s["queue_end"]
    fields = [
        _field("__\U0001f3a7 Finished__",
               f"{s['listened_count']} episodes\n{rr.wrap_duration(dur(s['listened_seconds']))}"),
        _field("__\U0001f4e5 Queue__",
               f"{qs['queue_count']} \u2192 {qe['queue_count']} episodes\n{signed(s['queue_seconds_delta'])}"),
        _field("__\u23f3 Behind__",
               f"{rr.wrap_duration(dur(qe['behind_seconds']))}\n{behind_phrase(s['behind_delta'])}"),
        _field("__\U0001f4ec New arrivals__", f"{rr.wrap_duration(dur(s['arrivals_seconds']))}"),
    ]
    hl = highlights(s, tz)
    if hl:
        fields.append(_field("__\u2728 Highlights__", "\n".join(f"{i} {l}" for i, l in hl)[:1000], inline=False))
    for dsh in s["dashes"]:
        fields.append(_field(
            f"__\U0001f690 Dash {clock(dsh['start'], tz)}\u2013{clock(dsh['end'], tz)}__",
            f"finished {dur(dsh['listened_seconds'])} of audio\nqueue {signed(dsh['queue_seconds_delta'])}\n"
            f"behind {behind_phrase(dsh['behind_delta'])}", inline=False))
    peak = max([w["played"] for w in s["windows"]] + [1])
    lines = [
        f"`{clock(w['snap'], tz):>7}` {label_name(w['snap'])} `{bar_chat(w['played'], peak):<10}` {window_sentence(w)}"
        for w in s["windows"]
    ]
    shape = "\n".join(lines[-9:])
    fields.append(_field("__\U0001f552 The shape of your day__", shape[:1000], inline=False))
    rows = pile_rows(s)
    if rows:
        piling = "\n".join(f"**{r['podcast'][:36]}** \u00b7 {pile_line(r)}" for r in rows[:8])
        if len(rows) > 8:
            piling += f"\n\u2026 and {len(rows) - 8} more"
        fields.append(_field(f"__\U0001f4da What's piling up ({len(rows)} shows, longest-waiting first)__",
                             piling[:1000], inline=False))
    if s.get("now_playing"):
        np = s["now_playing"]
        fields.append(_field("__\u25b6 Now playing__", f"{np['title']} \u2014 {np['podcast']}"[:300], inline=False))
    return _flow_embed(f"Daily Flow \u2014 {pretty_day(s['day'])}", day_headline(s), fields,
                       generated, rr.grade_int(qe.get("grade") or "D"), tz)


def build_discord_week(w, generated=None, tz=None):
    generated = generated or utcnow()
    if not w["days"]:
        return {"content": "Weekly Flow: no snapshots recorded this week."}
    best = w["best"]
    fields = [
        _field("__\U0001f3a7 Finished__",
               f"{rr.wrap_duration(dur(w['listened_seconds']))}\n{dur_n(w['average_seconds'])} a day over {len(w['days'])} days"),
        _field("__\U0001f3c6 Best day__", f"{pretty_day(best['day'])}\n{dur_n(best['listened_seconds'], 3)}"),
        _field("__\U0001f4e5 Queue__",
               f"{w['queue_start']['queue_count']} \u2192 {w['queue_end']['queue_count']} episodes\nbehind {behind_phrase(w['behind_delta'])}"),
        _field("__\U0001f4ec New arrivals__", f"{rr.wrap_duration(dur(w['arrivals_seconds']))}"),
        _field("__\U0001f4c6 By day__", "\n".join(
            f"`{d['day'].strftime('%a %b %-d'):<8}` {dur_n(d['listened_seconds'])} \u00b7 {d['queue_end']['queue_count']} eps"
            for d in w["days"])[:1000], inline=False),
    ]
    first, last = w["days"][0]["day"], w["days"][-1]["day"]
    return _flow_embed(f"Weekly Flow \u2014 {first.strftime('%b %-d')} to {last.strftime('%b %-d')}",
                       week_headline(w), fields, generated,
                       rr.grade_int(w["queue_end"].get("grade") or "D"), tz)


def email_payload(subject, body_html):
    """Same SUBJECT / --- / body layout scripts/send_email.py reads."""
    return f"SUBJECT: {subject}\n---\n{body_html}"


# --- running ---------------------------------------------------------------

def run_query():
    """One query against the Podcasts DB, exactly like `make run`. Each run
    advances the 'since last check' window, so one snapshot = one query."""
    proc = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "podcast_summary.py")],
        capture_output=True, text=True, cwd=SCRIPT_DIR,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"podcast_summary.py failed ({proc.returncode})")
    with open(RUN_JSON, "w") as f:
        f.write(proc.stdout)
    return json.loads(proc.stdout)


def _pipe(render_args, script):
    rendered = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "render_report.py"), RUN_JSON] + render_args,
        capture_output=True, text=True, cwd=SCRIPT_DIR,
    )
    if rendered.returncode != 0:
        sys.stderr.write(rendered.stderr)
        return False
    return _send(rendered.stdout, script)


def _send(text, script):
    sent = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "scripts", script)],
        input=text, capture_output=True, text=True, cwd=SCRIPT_DIR,
    )
    sys.stdout.write(sent.stdout)
    if sent.returncode != 0:
        sys.stderr.write(sent.stderr)
    return sent.returncode == 0


def write_html_report():
    rendered = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "render_report.py"), RUN_JSON, "html"],
        capture_output=True, text=True, cwd=SCRIPT_DIR,
    )
    if rendered.returncode == 0:
        path = os.path.join(SCRIPT_DIR, "reports", "podcast_report.html")
        with open(path, "w") as f:
            f.write(rendered.stdout)
        return path
    sys.stderr.write(rendered.stderr)
    return None


def _flow_email_enabled():
    return os.environ.get("FLOW_EMAIL", "").strip().lower() in ("1", "true", "yes", "on")


def write_flow_pages(history, day, d, generated, tz=None):
    """The web reports: reports/daily_flow.html is always today's flow so far
    plus the latest queue; reports/flow/<date>.html keeps each day. Sunday's
    evening snapshot also writes reports/weekly_flow.html."""
    os.makedirs(os.path.join(REPORTS_DIR, "flow"), exist_ok=True)
    s = summarize_day(history, day, tz)
    page = render_day_html(s, tz, generated, d)
    daily = os.path.join(REPORTS_DIR, "daily_flow.html")
    archive = os.path.join(REPORTS_DIR, "flow", f"{day.isoformat()}.html")
    for path in (daily, archive):
        with open(path, "w") as f:
            f.write(page)
    paths = {"day": daily, "day_archive": archive}
    if day.weekday() == 6:
        w = summarize_week(history, day, tz)
        path = os.path.join(REPORTS_DIR, "weekly_flow.html")
        with open(path, "w") as f:
            f.write(render_week_html(w, tz, generated, d))
        paths["week"] = path
    return paths, s


def email_day(s, page_html, label):
    qe = s["queue_end"]
    subject = (
        f"{label} \u2014 Daily Flow, {pretty_day(s['day'])}: finished {dur(s['listened_seconds'])}, "
        f"queue {qe['queue_count']} episodes"
    )
    return _send(email_payload(subject, page_html), "send_email.py")


def take_snapshot(label, quiet=False, open_report=False, history_path=None):
    d = run_query()
    snap = snapshot_from_run(d, label)
    append_snapshot(snap, history_path)
    history = load_history(history_path)
    generated = parse_utc(d["generated_at"])
    day = day_of(local_dt(snap))
    write_html_report()
    paths, s = write_flow_pages(history, day, d, generated)
    cfg_label = d.get("config", {}).get("label") or "Podcast Queue Report"
    print(f"\U0001f4f8 {label} snapshot recorded: {snap['queue_count']} episodes, "
          f"played {dur(snap['played_seconds'])} since the last one")
    print(f"\U0001f310 Web report: {paths['day']}")
    if open_report:
        subprocess.run(["open", paths["day"]])
    if not quiet and label != "pulse":
        _pipe(["discord"], "post_discord.py")
        if label in DISCORD_FLOW_LABELS:
            _send(json.dumps(build_discord_day(s, generated)), "post_discord.py")
        if label == "evening" and day.weekday() == 6:
            w = summarize_week(history, day)
            _send(json.dumps(build_discord_week(w, generated)), "post_discord.py")
    if label == "evening" and _flow_email_enabled():
        email_day(s, render_day_html(s, None, generated, d), cfg_label)
        if "week" in paths:
            w = summarize_week(history, day)
            _send(email_payload(f"{cfg_label} \u2014 Weekly Flow: finished {dur(w['listened_seconds'])}",
                                render_week_html(w, None, generated, d)), "send_email.py")
    return snap


def cmd_tick(args):
    now_utc = (
        datetime.datetime.fromisoformat(args.now).astimezone(datetime.timezone.utc)
        if args.now else datetime.datetime.now(datetime.timezone.utc)
    )
    history = load_history()
    action = decide_tick(history, now_utc)
    local = now_utc.astimezone(ps.LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
    if action is None:
        print(f"NOOP at {local}: nothing due")
        return 0
    if args.dry_run:
        print(f"DRY RUN at {local}: would take the {action} snapshot")
        return 0
    take_snapshot(action)
    return 0


def _arg_day(value):
    if value:
        return datetime.date.fromisoformat(value)
    return day_of(datetime.datetime.now(ps.LOCAL_TZ))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("snapshot", help="query once, record it, deliver")
    p.add_argument("--label", default="manual", choices=LABELS)
    p.add_argument("--quiet", action="store_true", help="skip the Discord post")
    p.add_argument("--open", action="store_true", help="open the HTML report")

    for name in ("day", "week"):
        p = sub.add_parser(name, help=f"render the {name} flow from history")
        p.add_argument("--date")
        p.add_argument("--format", choices=("chat", "html", "email"), default="chat")
        p.add_argument("--no-queue", action="store_true",
                       help="leave the unplayed queue off html/email (it is added from the last run's JSON by default)")

    p = sub.add_parser("tag-dash", help="label the readings nearest a Dash's real start and end")
    p.add_argument("--date", help="the day the Dash started (YYYY-MM-DD), default today")
    p.add_argument("--start", required=True, help="when the Dash really started, e.g. 10:11am")
    p.add_argument("--end", required=True, help="when the Dash really ended, e.g. 6:18pm")
    p.add_argument("--within", type=int, default=60, help="how many minutes away a reading may be")

    p = sub.add_parser("tick", help="hourly scheduler entry point")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--now", help="ISO time with offset, for testing")

    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        take_snapshot(args.label, quiet=args.quiet, open_report=args.open)
        return 0
    if args.cmd == "tick":
        return cmd_tick(args)
    if args.cmd == "tag-dash":
        history = load_history()
        rows, messages, ok = tag_dash(history, _arg_day(args.date), parse_clock(args.start),
                                      parse_clock(args.end), within_minutes=args.within)
        for m in messages:
            print(m)
        if ok:
            write_history(rows)
        return 0 if ok else 1

    history = load_history()
    day = _arg_day(args.date)
    d = (json.load(open(RUN_JSON))
         if args.format in ("html", "email") and not args.no_queue and os.path.exists(RUN_JSON) else None)
    if args.cmd == "day":
        s = summarize_day(history, day)
        if args.format == "chat":
            print(render_day_chat(s))
        elif args.format == "html":
            print(render_day_html(s, None, None, d))
        else:
            print(email_payload(f"Daily Flow, {pretty_day(day)}", render_day_html(s, None, None, d)))
    else:
        w = summarize_week(history, day)
        if args.format == "chat":
            print(render_week_chat(w))
        elif args.format == "html":
            print(render_week_html(w, None, None, d))
        else:
            print(email_payload("Weekly Flow", render_week_html(w, None, None, d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
