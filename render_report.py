import json, sys, datetime, html
from zoneinfo import ZoneInfo

PLAYED_LABELS = [
    ("since_last_run", "Since last check"),
    ("yesterday", "Yesterday"),
    ("past_week", "Past week"),
    ("past_month", "This month"),
    ("all_time", "All time"),
]


_SHORT_DURATION_UNITS = [
    ("year", 365 * 86400),
    ("month", 30 * 86400),
    ("week", 7 * 86400),
    ("day", 86400),
    ("hr", 3600),
    ("min", 60),
    ("second", 1),
]

def fmt_duration_short(seconds, max_units=2):
    """Same breakdown as podcast_summary.fmt_duration, but caps at the
    `max_units` biggest non-zero units instead of spelling out every unit
    down to seconds — e.g. "2 weeks 1 day" instead of "2 weeks 1 day 21 hrs
    42 mins 17 seconds". Discord embed fields are small and card-like, so
    the long form wraps awkwardly."""
    seconds = int(round(seconds or 0))
    if seconds <= 0:
        return "0 seconds"
    parts = []
    remaining = seconds
    for name, size in _SHORT_DURATION_UNITS:
        if len(parts) >= max_units:
            break
        value = remaining // size
        remaining -= value * size
        if value > 0:
            label = name if value == 1 else (f"{name}s" if name != "hr" else "hrs")
            if name == "hr":
                label = "hr" if value == 1 else "hrs"
            parts.append(f"{value:,} {label}")
    return " ".join(parts)

GRADE_COLORS = {
    "A+": 0x0d9488, "A": 0x0d9488, "A-": 0x16a34a,
    "B+": 0x65a30d, "B": 0xca8a04, "B-": 0xd97706,
    "C+": 0xea580c, "C": 0xea580c, "C-": 0xdc2626,
    "D+": 0xdc2626, "D": 0xb91c1c, "D-": 0x991b1b, "F": 0x7f1d1d,
}

def local_tz(d):
    # Timezone comes from the JSON's config (itself sourced from the
    # REPORT_TIMEZONE env var / .env in podcast_summary.py), so both scripts
    # always agree on the same zone without duplicating config-loading logic.
    return ZoneInfo(d.get("config", {}).get("timezone") or "UTC")

def human_date(d, iso):
    # Stored timestamps are naive UTC; convert to local time for display.
    dt = datetime.datetime.fromisoformat(iso).replace(tzinfo=datetime.timezone.utc)
    dt_local = dt.astimezone(local_tz(d))
    return dt_local.strftime("%a %b %-d, %-I:%M%p %Z")

def pluralize(n, noun, n_fmt=None):
    disp = n_fmt if n_fmt is not None else n
    return f"{disp} {noun}" if n == 1 else f"{disp} {noun}s"

def fmt_days_decimal(days):
    """e.g. 2.5 -> '2.5', 3.0 -> '3' — used for the email subject line."""
    if abs(days - round(days)) < 0.01:
        return str(int(round(days)))
    return f"{days:.1f}"

def up_next_sentence(q):
    if not q["episodes"]:
        return "The queue is empty — you're all caught up!"
    ep = q["episodes"][0]
    return (f'There are currently {pluralize(q["count"], "episode", q["count_fmt"])} in the queue for a total time of '
            f'{q["total_fmt"]} — with the next episode to complete being "{ep["title"]}" '
            f'({q["up_next_remaining_fmt"]} left to finish playing).')

def build_chat_summary(d):
    q = d["queue"]
    p = d["played"]
    lines = []
    lines.append(d["emoji_header"])
    lines.append(f'That papa is {q["days_behind_fmt"]} days behind the times — Grade: {q["grade"]} 🎧')
    lines.append("")
    lines.append(up_next_sentence(q))
    lines.append("")
    lines.append("Played:")
    for key, label in PLAYED_LABELS:
        if key in p:
            lines.append(f'  {label}: {pluralize(p[key]["count"], "ep", p[key]["count_fmt"])}, {p[key]["total_fmt"]}')
    return "\n".join(lines)

def build_discord(d):
    """Returns a full Discord webhook payload (dict) with an embed laid out
    like the HTML report's stat cards: a queue summary field up top, then
    one card-style field per Played window, using short (2-unit) durations
    so long spans like "All time" don't wrap into a wall of text."""
    q = d["queue"]
    p = d["played"]

    description = f'Grade **{q["grade"]}** — {q["days_behind_fmt"]} days behind 🎧\n\n'
    if q["episodes"]:
        description += f'**Up next:** "{q["episodes"][0]["title"]}"'
    else:
        description += "**Up next:** queue is empty — you're all caught up!"

    fields = [{
        "name": "In queue",
        "value": f'{pluralize(q["count"], "episode", q["count_fmt"])}\n{fmt_duration_short(q["total_seconds"])}',
        "inline": True,
    }]
    for key, label in PLAYED_LABELS:
        if key in p:
            fields.append({
                "name": label,
                "value": f'{pluralize(p[key]["count"], "episode", p[key]["count_fmt"])}\n{fmt_duration_short(p[key]["total_seconds"])}',
                "inline": True,
            })

    embed = {
        "title": f'{d["emoji_header"]}',
        "description": description,
        "color": GRADE_COLORS.get(q["grade"], 0x334155),
        "fields": fields,
        "footer": {"text": "Podcast Queue Report"},
        "timestamp": d["generated_at"],
    }
    return {"embeds": [embed]}

def build_sms(d):
    q = d["queue"]
    return (f'{d["emoji_header"]}\n'
            f'That papa is {q["days_behind_fmt"]} days behind the times — Grade: {q["grade"]} 🎧.  '
            f'{up_next_sentence(q)}')

def build_email(d):
    q = d["queue"]
    p = d["played"]
    label = d.get("config", {}).get("label") or "Podcast Queue Report"
    subject = f'{label} — Grade {q["grade"]} ({fmt_days_decimal(q["days_behind"])} days behind)'
    lines = []
    lines.append(d["emoji_header"])
    lines.append(f'That papa is {q["days_behind_fmt"]} days behind the times — Grade: {q["grade"]} 🎧')
    lines.append("")
    lines.append(up_next_sentence(q))
    lines.append("")
    lines.append("PLAYED")
    for key, label in PLAYED_LABELS:
        if key in p:
            lines.append(f'{label}: {pluralize(p[key]["count"], "episode", p[key]["count_fmt"])}, {p[key]["total_fmt"]}')
    lines.append("")
    lines.append("Full episode-by-episode breakdown is attached as an HTML report.")
    lines.append("")
    signoff = d.get("config", {}).get("signoff_name")
    if signoff:
        lines.append(f"— {signoff}")
    return subject, "\n".join(lines)

def build_html(d):
    q = d["queue"]
    p = d["played"]

    def duration_html(total_fmt):
        # fmt_duration joins "N\u00a0unit" pairs with plain spaces, so
        # splitting on a plain space gives us each pair intact. When there
        # are more than 3 (i.e. more granular than hr/min/sec), stack each
        # on its own line instead of letting it wrap mid-sentence.
        pairs = total_fmt.split(" ")
        escaped = [html.escape(p) for p in pairs]
        if len(escaped) > 3:
            return "<br>".join(escaped)
        return " ".join(escaped)

    def stat_card(label, stats):
        noun = "episode" if stats["count"] == 1 else "episodes"
        return f'''
        <div class="card">
          <div class="card-label">{html.escape(label)}</div>
          <div class="card-count-row">
            <span class="card-count">{stats["count_fmt"]}</span>
            <span class="card-count-unit">{noun}</span>
          </div>
          <div class="card-sub">{duration_html(stats["total_fmt"])}</div>
        </div>'''

    played_cards = ""
    for key, label in PLAYED_LABELS:
        if key in p:
            played_cards += stat_card(label, p[key])

    rows = ""
    for i, e in enumerate(q["episodes"]):
        pub = datetime.datetime.fromisoformat(e["pubdate"]).replace(tzinfo=datetime.timezone.utc).astimezone(local_tz(d))
        badge = ' <span class="up-next">▶ UP NEXT</span>' if i == 0 else ""
        title_html = html.escape(e["title"])
        if e.get("episode_url"):
            title_html = f'<a href="{html.escape(e["episode_url"])}" target="_blank">{title_html}</a>'
        pod_html = html.escape(e["podcast"])
        if e.get("podcast_url"):
            pod_html = f'<a href="{html.escape(e["podcast_url"])}" target="_blank">{pod_html}</a>'
        rows += f'''
        <tr class="{"up-next-row" if i == 0 else ""}">
          <td class="ep-title">{title_html}{badge}<div class="ep-pod">{pod_html}</div></td>
          <td class="ep-date">{pub.strftime("%a %b %-d")}</td>
          <td class="ep-dur">{e["duration_fmt"]}</td>
        </tr>'''

    grade_colors = {
        "A+": "#0d9488", "A": "#0d9488", "A-": "#16a34a",
        "B+": "#65a30d", "B": "#ca8a04", "B-": "#d97706",
        "C+": "#ea580c", "C": "#ea580c", "C-": "#dc2626",
        "D+": "#dc2626", "D": "#b91c1c", "D-": "#991b1b", "F": "#7f1d1d",
    }
    grade_color = grade_colors.get(q["grade"], "#334155")

    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Podcast Queue Report</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f8fafc; margin: 0; padding: 32px; color: #1e293b; }}
  .container {{ max-width: 720px; margin: 0 auto; }}
  .emoji-strip {{ font-size: 28px; letter-spacing: 2px; text-align: center; margin-bottom: 8px; line-height: 1.4; }}
  .headline {{ text-align: center; font-size: 20px; font-weight: 600; margin-bottom: 28px; }}
  .grade-badge {{ display: inline-block; background: {grade_color}; color: white; border-radius: 8px; padding: 2px 12px; font-weight: 700; }}
  .queue-summary {{ background: white; border-radius: 12px; padding: 20px 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .queue-summary h2 {{ margin: 0 0 4px; font-size: 15px; text-transform: uppercase; letter-spacing: 0.5px; color: #64748b; }}
  .queue-summary .big {{ font-size: 32px; font-weight: 700; }}
  .queue-summary .sub {{ color: #64748b; font-size: 14px; margin-top: 4px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 12px; margin-bottom: 28px; }}
  .card {{ background: white; border-radius: 12px; padding: 18px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .card-label {{ font-size: 11px; font-weight: 600; text-transform: uppercase; color: #94a3b8; letter-spacing: 0.6px; margin-bottom: 8px; }}
  .card-count-row {{ display: flex; align-items: baseline; gap: 6px; margin-bottom: 8px; }}
  .card-count {{ font-size: 28px; font-weight: 700; line-height: 1; }}
  .card-count-unit {{ font-size: 13px; font-weight: 600; color: #94a3b8; }}
  .card-sub {{ font-size: 13px; color: #64748b; line-height: 1.7; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th {{ text-align: left; font-size: 12px; text-transform: uppercase; color: #94a3b8; padding: 12px 16px; border-bottom: 1px solid #e2e8f0; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid #f1f5f9; vertical-align: top; font-size: 14px; }}
  .ep-title {{ font-weight: 600; }}
  .ep-title a {{ color: #1e293b; text-decoration: none; }}
  .ep-title a:hover {{ text-decoration: underline; }}
  .ep-pod {{ font-weight: 400; color: #64748b; font-size: 12px; margin-top: 2px; }}
  .ep-pod a {{ color: #64748b; text-decoration: none; }}
  .ep-pod a:hover {{ text-decoration: underline; }}
  .up-next {{ display: inline-block; background: #dbeafe; color: #1d4ed8; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; border-radius: 4px; padding: 1px 6px; margin-left: 6px; vertical-align: middle; }}
  .up-next-row {{ background: #f8fafc; }}
  .ep-date, .ep-dur {{ color: #475569; white-space: nowrap; }}
  .footer {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="container">
  <div class="emoji-strip">{d["emoji_header"]}</div>
  <div class="headline">That papa is {q["days_behind_fmt"]} days behind the times &mdash; Grade: <span class="grade-badge">{q["grade"]}</span> 🎧</div>

  <div class="queue-summary">
    <h2>Unplayed Queue</h2>
    <div class="big">{q["count_fmt"]} episodes &middot; {q["total_fmt"]}</div>
    <div class="sub">Oldest: {human_date(d, q["oldest_date"])} ({q["days_behind_fmt"]} days behind)</div>
  </div>

  <div class="cards">
    {played_cards}
  </div>

  <table>
    <tr><th>Episode</th><th>Published</th><th>Length</th></tr>
    {rows}
  </table>

  <div class="footer">Generated {human_date(d, d["generated_at"])}</div>
</div>
</body>
</html>'''

if __name__ == "__main__":
    data = json.load(open(sys.argv[1]))
    mode = sys.argv[2] if len(sys.argv) > 2 else "chat"
    if mode == "chat":
        print(build_chat_summary(data))
    elif mode == "sms":
        print(build_sms(data))
    elif mode == "discord":
        print(json.dumps(build_discord(data)))
    elif mode == "email":
        subject, body = build_email(data)
        print(f"SUBJECT: {subject}")
        print("---")
        print(body)
    elif mode == "html":
        print(build_html(data))
