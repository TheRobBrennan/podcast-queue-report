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
LABELS = ("morning", "dash-start", "dash-end", "evening", "manual")

DAY_STARTS_HOUR = 4
MORNING_FALLBACK_HOUR = 11
MORNING_FALLBACK_END_HOUR = 17   # past this, a "morning" snapshot would be a lie
EVENING_FALLBACK_HOUR = 22
MIN_GAP_MINUTES = 20
REPORTS_DIR = os.path.join(SCRIPT_DIR, "reports")
DISCORD_FLOW_LABELS = ("dash-end", "evening")


# --- history ---------------------------------------------------------------

def parse_utc(iso):
    """The JSON's timestamps are naive UTC."""
    dt = datetime.datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt


def snapshot_from_run(d, label, source="live"):
    q = d["queue"]
    since = d["played"]["since_last_run"]
    return {
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


def summarize_day(history, day, tz=None):
    snaps = day_snapshots(history, day, tz)
    out = {"day": day, "snapshots": snaps, "dashes": []}
    if not snaps:
        return out
    first, last = snaps[0], snaps[-1]
    out["listened_seconds"] = sum(s["played_seconds"] for s in snaps)
    out["listened_count"] = sum(s["played_count"] for s in snaps)
    out["queue_start"], out["queue_end"] = first, last
    out["queue_count_delta"] = last["queue_count"] - first["queue_count"]
    out["queue_seconds_delta"] = last["queue_seconds"] - first["queue_seconds"]
    out["behind_delta"] = last["behind_seconds"] - first["behind_seconds"]
    out["arrivals_seconds"] = sum(
        arrivals_seconds(a, b) for a, b in zip(snaps, snaps[1:])
    )

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
    for s in snaps:
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
    return out


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
    """What the hourly tick should do right now: "morning", "evening", or
    None. Uses the Mac's local clock at run time, so it follows travel
    between time zones without reloading the launchd agent."""
    now_local = now_utc.astimezone(tz or ps.LOCAL_TZ)
    day = day_of(now_local)
    labels = {s["label"] for s in day_snapshots(history, day, tz)}
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
    return None


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


def pretty_day(day):
    return day.strftime("%A, %b %-d")


def behind_phrase(delta):
    if round(delta) == 0:
        return "no change"
    return f"{'improved' if delta < 0 else 'slipped'} {dur(delta)}"


def mood(delta):
    """The tagline: how the day treated the backlog."""
    if round(delta) < 0:
        return "and the furballs are proud"
    if round(delta) == 0:
        return "and held the line"
    return "and the backlog crept up"


def day_headline(s):
    return f"That papa listened to {dur(s['listened_seconds'])} today {mood(s['behind_delta'])} 🎧"


def week_headline(w):
    return f"That papa listened to {dur(w['listened_seconds'])} this week {mood(w['behind_delta'])} 🎧"


def emoji_row_html():
    return "".join(
        f'<span style="display:inline-block;margin:0 2px;">{e}</span>'
        for e in ps.EMOJI_POOL
    )


def render_day_chat(s, tz=None):
    if not s["snapshots"]:
        return f"🎧 Daily flow — {pretty_day(s['day'])}\nNo snapshots recorded."
    qs, qe = s["queue_start"], s["queue_end"]
    n = len(s["snapshots"])
    lines = [
        ps.emoji_header(),
        day_headline(s),
        f"🎧 Daily flow — {pretty_day(s['day'])}",
        f"Listened: {dur(s['listened_seconds'])} · {s['listened_count']} episodes played · {n} snapshots",
        f"Queue: {qs['queue_count']} → {qe['queue_count']} episodes "
        f"({signed(s['queue_seconds_delta'])})",
        f"Behind: {dur(qs['behind_seconds'])} → {dur(qe['behind_seconds'])} "
        f"({behind_phrase(s['behind_delta'])})",
        f"New arrivals (estimated): {dur(s['arrivals_seconds'])}",
    ]
    for dsh in s["dashes"]:
        lines.append(
            f"🚐 Dash {clock(dsh['start'], tz)}–{clock(dsh['end'], tz)}: "
            f"listened {dur(dsh['listened_seconds'])}, queue {signed(dsh['queue_seconds_delta'])}, "
            f"behind {behind_phrase(dsh['behind_delta'])}"
        )
    if s["drought"] and s["drought"][0] >= 3 * 3600:
        span, a, b = s["drought"]
        lines.append(
            f"Quietest stretch: {dur(span)}, "
            f"{local_dt({'ts': a}, tz).strftime('%-I:%M%p').lower()}–"
            f"{local_dt({'ts': b}, tz).strftime('%-I:%M%p').lower()}, nothing played"
        )
    lines.append("")
    for snap in s["snapshots"]:
        lines.append(
            f"{clock(snap, tz):>8}  {snap['label']:<10}  {snap['queue_count']} eps  "
            f"{dur_n(snap['behind_seconds'], 3):<26} behind  played {dur_n(snap['played_seconds'])}"
        )
    return "\n".join(lines)


def render_week_chat(w, tz=None):
    if not w["days"]:
        return "🎧 Weekly flow\nNo snapshots recorded this week."
    first, last = w["days"][0]["day"], w["days"][-1]["day"]
    best = w["best"]
    lines = [
        ps.emoji_header(),
        week_headline(w),
        f"🎧 Weekly flow — {first.strftime('%b %-d')} to {last.strftime('%b %-d')}",
        f"Listened: {dur(w['listened_seconds'])} across {len(w['days'])} days, "
        f"{dur(w['average_seconds'])} a day on average",
        f"Best day: {pretty_day(best['day'])}, {dur(best['listened_seconds'])}",
        f"Queue: {w['queue_start']['queue_count']} → {w['queue_end']['queue_count']} episodes, "
        f"behind {behind_phrase(w['behind_delta'])}",
        f"New arrivals (estimated): {dur(w['arrivals_seconds'])}",
        "",
    ]
    for d in w["days"]:
        lines.append(
            f"{d['day'].strftime('%a %b %-d'):<10} listened {dur(d['listened_seconds']):<28} "
            f"queue {d['queue_end']['queue_count']} eps"
        )
    return "\n".join(lines)


def _card(label, value, sub=""):
    return (
        '<td width="50%" valign="top" style="padding:0 8px 16px 0;">'
        '<div style="background:#ffffff;border-radius:12px;padding:20px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.08);">'
        f'<div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#94a3b8;'
        f'letter-spacing:0.6px;margin-bottom:8px;">{html.escape(label)}</div>'
        f'<div style="font-size:22px;font-weight:700;line-height:1.2;">{html.escape(value)}</div>'
        f'<div style="font-size:13px;color:#64748b;margin-top:6px;">{html.escape(sub)}</div>'
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
    """The main report's body (grade line, now playing, the queue with artwork)
    for appending under the flow summary. Its own emoji row is dropped, since
    the flow page already opens with the whole cast."""
    page = rr.build_html(d)
    body = page.split("<body", 1)[1].split(">", 1)[1].rsplit("</body>", 1)[0]
    body = re.sub(
        r'<div style="text-align:center;font-size:28px;line-height:1.3;margin-bottom:16px;">.*?</div>',
        "", body, count=1, flags=re.S,
    )
    return (
        '<div style="border-top:2px dashed #cbd5e1;margin:40px 0 28px;"></div>'
        '<div style="text-align:center;font-size:13px;font-weight:600;color:#64748b;'
        'text-transform:uppercase;letter-spacing:0.6px;margin-bottom:20px;">The unplayed queue</div>'
        + body
    )


def _page(title, body, headline=None, generated=None, tz=None, queue_html=""):
    generated = generated or utcnow()
    headline_html = (
        f'<div style="text-align:center;font-size:20px;font-weight:600;margin-bottom:8px;">'
        f'{html.escape(headline)}</div>' if headline else ""
    )
    pill = generated_pill(generated, tz)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title></head>
<body style="margin:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#0f172a;">
<div style="max-width:720px;margin:0 auto;padding:32px 20px;">
<div style="text-align:center;font-size:25px;line-height:1.3;margin-bottom:16px;white-space:nowrap;">{emoji_row_html()}</div>
{headline_html}
<div style="text-align:center;font-size:13px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.6px;">{html.escape(title)}</div>
<div style="text-align:center;margin:10px 0 4px;">{pill}</div>
{body}
{queue_html}
<div style="text-align:center;margin-top:32px;">{pill}</div>
</div></body></html>"""


def render_day_html(s, tz=None, generated=None, d=None):
    title = f"Daily flow — {pretty_day(s['day'])}"
    if not s["snapshots"]:
        return _page(title, "<p>No snapshots recorded.</p>", None, generated, tz)
    qs, qe = s["queue_start"], s["queue_end"]
    cards = [
        _card("Listened", dur(s["listened_seconds"]),
              f"{s['listened_count']} episodes played"),
        _card("Queue", f"{qs['queue_count']} → {qe['queue_count']} episodes",
              signed(s["queue_seconds_delta"])),
        _card("Behind", dur(qe["behind_seconds"]),
              f"from {dur(qs['behind_seconds'])}, {behind_phrase(s['behind_delta'])}"),
        _card("New arrivals", dur(s["arrivals_seconds"]), "estimated"),
    ]
    rows = "".join(
        f"<tr>{cards[i]}{cards[i + 1]}</tr>" for i in range(0, len(cards), 2)
    )
    parts = [f'<table width="100%" cellpadding="0" cellspacing="0" style="margin-top:20px;">{rows}</table>']
    if s["dashes"]:
        items = ""
        for dsh in s["dashes"]:
            items += (
                f"<div style=\"margin:6px 0;\">🚐 <b>{clock(dsh['start'], tz)}–{clock(dsh['end'], tz)}</b> "
                f"listened {html.escape(dur(dsh['listened_seconds']))}, "
                f"queue {html.escape(signed(dsh['queue_seconds_delta']))}, "
                f"behind {html.escape(behind_phrase(dsh['behind_delta']))}</div>"
            )
        parts.append(
            '<div style="background:#fff;border-radius:12px;padding:16px 20px;margin-bottom:16px;'
            f'box-shadow:0 1px 3px rgba(0,0,0,0.08);"><div style="font-weight:700;">Dash windows</div>{items}</div>'
        )
    if s["drought"] and s["drought"][0] >= 3 * 3600:
        span, a, b = s["drought"]
        parts.append(
            '<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:12px;'
            'padding:14px 20px;margin-bottom:16px;font-size:14px;color:#92400e;">'
            f"Quietest stretch: {html.escape(dur(span))}, "
            f"{local_dt({'ts': a}, tz).strftime('%-I:%M%p').lower()}–"
            f"{local_dt({'ts': b}, tz).strftime('%-I:%M%p').lower()}, nothing played</div>"
        )
    body_rows = ""
    for snap in s["snapshots"]:
        body_rows += (
            "<tr>"
            f"<td style='padding:8px 12px;'>{clock(snap, tz)}</td>"
            f"<td style='padding:8px 12px;'>{html.escape(snap['label'])}</td>"
            f"<td style='padding:8px 12px;'>{snap['queue_count']} eps</td>"
            f"<td style='padding:8px 12px;'>{html.escape(dur_n(snap['behind_seconds'], 3))}</td>"
            f"<td style='padding:8px 12px;'>{html.escape(dur_n(snap['played_seconds']))}</td>"
            "</tr>"
        )
    parts.append(
        '<table width="100%" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:12px;'
        'box-shadow:0 1px 3px rgba(0,0,0,0.08);font-size:13px;">'
        '<tr style="text-align:left;color:#94a3b8;font-size:11px;text-transform:uppercase;letter-spacing:0.6px;">'
        "<th style='padding:12px;'>Time</th><th style='padding:12px;'>Snapshot</th>"
        "<th style='padding:12px;'>Queue</th><th style='padding:12px;'>Behind</th>"
        "<th style='padding:12px;'>Played in window</th></tr>"
        f"{body_rows}</table>"
    )
    return _page(title, "".join(parts), day_headline(s), generated, tz,
                 queue_section_html(d) if d else "")


def render_week_html(w, tz=None, generated=None, d=None):
    title = "Weekly flow"
    if not w["days"]:
        return _page(title, "<p>No snapshots recorded this week.</p>", None, generated, tz)
    best = w["best"]
    cards = [
        _card("Listened", dur(w["listened_seconds"]),
              f"{dur(w['average_seconds'])} a day over {len(w['days'])} days"),
        _card("Best day", dur(best["listened_seconds"]), pretty_day(best["day"])),
        _card("Queue", f"{w['queue_start']['queue_count']} → {w['queue_end']['queue_count']} episodes",
              f"behind {behind_phrase(w['behind_delta'])}"),
        _card("New arrivals", dur(w["arrivals_seconds"]), "estimated"),
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
        "<th style='padding:12px;'>Day</th><th style='padding:12px;'>Listened</th>"
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
        "footer": {"text": "Podcast Queue Report \u00b7 Daily flow"},
        "timestamp": generated.astimezone(datetime.timezone.utc).replace(tzinfo=None).isoformat(),
    }]}


def _field(name, value, inline=True):
    return {"name": name, "value": value or "-", "inline": inline}


def build_discord_day(s, generated=None, tz=None):
    generated = generated or utcnow()
    if not s["snapshots"]:
        return {"content": f"Daily flow, {pretty_day(s['day'])}: no snapshots recorded."}
    qs, qe = s["queue_start"], s["queue_end"]
    fields = [
        _field("__\U0001f3a7 Listened__",
               f"{s['listened_count']} episodes\n{rr.wrap_duration(dur(s['listened_seconds']))}"),
        _field("__\U0001f4e5 Queue__",
               f"{qs['queue_count']} \u2192 {qe['queue_count']} episodes\n{signed(s['queue_seconds_delta'])}"),
        _field("__\u23f3 Behind__",
               f"{rr.wrap_duration(dur(qe['behind_seconds']))}\n{behind_phrase(s['behind_delta'])}"),
        _field("__\U0001f4ec New arrivals__", f"{rr.wrap_duration(dur(s['arrivals_seconds']))}\nestimated"),
    ]
    for dsh in s["dashes"]:
        fields.append(_field(
            f"__\U0001f690 Dash {clock(dsh['start'], tz)}\u2013{clock(dsh['end'], tz)}__",
            f"listened {dur(dsh['listened_seconds'])}\nqueue {signed(dsh['queue_seconds_delta'])}\n"
            f"behind {behind_phrase(dsh['behind_delta'])}", inline=False))
    if s["drought"] and s["drought"][0] >= 3 * 3600:
        span, a, b = s["drought"]
        fields.append(_field("__\U0001f634 Quietest stretch__",
                             f"{dur(span)}, "
                             f"{local_dt({'ts': a}, tz).strftime('%-I:%M%p').lower()}\u2013"
                             f"{local_dt({'ts': b}, tz).strftime('%-I:%M%p').lower()}", inline=False))
    timeline = "\n".join(
        f"`{clock(x, tz):>7}` {x['label']} \u00b7 {x['queue_count']} eps \u00b7 played {dur_n(x['played_seconds'])}"
        for x in s["snapshots"]
    )
    fields.append(_field("__\U0001f552 Timeline__", timeline[:1000], inline=False))
    return _flow_embed(f"Daily flow \u2014 {pretty_day(s['day'])}", day_headline(s), fields,
                       generated, rr.grade_int(qe.get("grade") or "D"), tz)


def build_discord_week(w, generated=None, tz=None):
    generated = generated or utcnow()
    if not w["days"]:
        return {"content": "Weekly flow: no snapshots recorded this week."}
    best = w["best"]
    fields = [
        _field("__\U0001f3a7 Listened__",
               f"{rr.wrap_duration(dur(w['listened_seconds']))}\n{dur_n(w['average_seconds'])} a day over {len(w['days'])} days"),
        _field("__\U0001f3c6 Best day__", f"{pretty_day(best['day'])}\n{dur_n(best['listened_seconds'], 3)}"),
        _field("__\U0001f4e5 Queue__",
               f"{w['queue_start']['queue_count']} \u2192 {w['queue_end']['queue_count']} episodes\nbehind {behind_phrase(w['behind_delta'])}"),
        _field("__\U0001f4ec New arrivals__", f"{rr.wrap_duration(dur(w['arrivals_seconds']))}\nestimated"),
        _field("__\U0001f4c6 By day__", "\n".join(
            f"`{d['day'].strftime('%a %b %-d'):<8}` {dur_n(d['listened_seconds'])} \u00b7 {d['queue_end']['queue_count']} eps"
            for d in w["days"])[:1000], inline=False),
    ]
    first, last = w["days"][0]["day"], w["days"][-1]["day"]
    return _flow_embed(f"Weekly flow \u2014 {first.strftime('%b %-d')} to {last.strftime('%b %-d')}",
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
        f"{label} \u2014 Daily flow, {pretty_day(s['day'])}: listened {dur(s['listened_seconds'])}, "
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
    if not quiet:
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
            _send(email_payload(f"{cfg_label} \u2014 Weekly flow: listened {dur(w['listened_seconds'])}",
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
        p.add_argument("--with-queue", action="store_true",
                       help="append the latest queue report (reads the last run's JSON)")

    p = sub.add_parser("tick", help="hourly scheduler entry point")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--now", help="ISO time with offset, for testing")

    args = parser.parse_args(argv)
    if args.cmd == "snapshot":
        take_snapshot(args.label, quiet=args.quiet, open_report=args.open)
        return 0
    if args.cmd == "tick":
        return cmd_tick(args)

    history = load_history()
    day = _arg_day(args.date)
    d = json.load(open(RUN_JSON)) if args.with_queue and os.path.exists(RUN_JSON) else None
    if args.cmd == "day":
        s = summarize_day(history, day)
        if args.format == "chat":
            print(render_day_chat(s))
        elif args.format == "html":
            print(render_day_html(s, None, None, d))
        else:
            print(email_payload(f"Daily flow, {pretty_day(day)}", render_day_html(s, None, None, d)))
    else:
        w = summarize_week(history, day)
        if args.format == "chat":
            print(render_week_chat(w))
        elif args.format == "html":
            print(render_week_html(w, None, None, d))
        else:
            print(email_payload("Weekly flow", render_week_html(w, None, None, d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
