import json, sys, datetime, html
from zoneinfo import ZoneInfo

PLAYED_LABELS = [
    ("since_last_run", "Since last check"),
    ("yesterday", "Yesterday"),
    ("past_week", "Past week"),
    ("past_month", "This month"),
    ("all_time", "All time"),
]


def wrap_duration(total_fmt, units_per_line=2):
    """Groups an already-formatted duration string (e.g. "1 year 1 week 1
    day 9 hrs 25 mins 28 seconds", from podcast_summary.fmt_duration) into
    lines of at most `units_per_line` unit-pairs each, joined with real
    line breaks — full precision kept, just wrapped instead of trailing off
    into one long line. Each "N\u00a0unit" pair uses a non-breaking space
    internally (never split mid-pair); only the plain space *between*
    pairs is a valid break point, which is exactly what str.split(" ")
    respects here."""
    pairs = total_fmt.split(" ")
    return "\n".join(
        " ".join(pairs[i:i + units_per_line])
        for i in range(0, len(pairs), units_per_line)
    )

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


def now_playing_sentence(d):
    np = d.get("now_playing")
    if not np:
        return None
    where = f' on {np["podcast"]}' if np["podcast"] else ""
    return f'▶️ Now playing: "{np["title"]}"{where} — {np["remaining_fmt"]} left'

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
    now_playing = now_playing_sentence(d)
    if now_playing:
        lines.append(now_playing)
        lines.append("")
    lines.append(up_next_sentence(q))
    lines.append("")
    lines.append("Played:")
    for key, label in PLAYED_LABELS:
        if key in p:
            lines.append(f'  {label}: {pluralize(p[key]["count"], "ep", p[key]["count_fmt"])}, {p[key]["total_fmt"]}')
    return "\n".join(lines)

def _discord_link(text, url):
    """Discord markdown link, or plain text if there's nowhere to link to."""
    return f'[{text}]({url})' if url else text

def build_discord(d):
    """Returns a full Discord webhook payload (dict) with an embed laid out
    like the HTML report's stat cards: a queue summary field up top, then
    one card-style field per Played window. Durations keep full precision
    (podcast_summary.fmt_duration's complete breakdown) but wrap at two
    unit-pairs per line via wrap_duration so long spans like "All time"
    stay readable instead of trailing off in one long line."""
    q = d["queue"]
    p = d["played"]

    description = f'Grade **{q["grade"]}** — {q["days_behind_fmt"]} days behind 🎧\n\n'
    np = d.get("now_playing")
    if np:
        title_link = _discord_link(f'"{np["title"]}"', np.get("episode_url"))
        where = f' — {_discord_link(np["podcast"], np.get("podcast_url"))}' if np["podcast"] else ""
        description += f'🟢 **Now playing:** {title_link}{where} ({np["remaining_fmt"]} left)\n\n'
    if q["episodes"]:
        ep = q["episodes"][0]
        title_link = _discord_link(f'"{ep["title"]}"', ep.get("episode_url"))
        where = f' — {_discord_link(ep["podcast"], ep.get("podcast_url"))}' if ep.get("podcast") else ""
        description += f'**Up next:** {title_link}{where}'
    else:
        description += "**Up next:** queue is empty — you're all caught up!"

    field_icons = {
        "queue": "📥", "since_last_run": "⏱️", "yesterday": "📆",
        "past_week": "🗓️", "past_month": "📅", "all_time": "⏳",
    }
    fields = [{
        "name": f'{field_icons["queue"]} In queue',
        "value": f'{pluralize(q["count"], "episode", q["count_fmt"])}\n{wrap_duration(q["total_fmt"])}',
        "inline": True,
    }]
    for key, label in PLAYED_LABELS:
        if key in p:
            fields.append({
                "name": f'{field_icons.get(key, "")} {label}'.strip(),
                "value": f'{pluralize(p[key]["count"], "episode", p[key]["count_fmt"])}\n{wrap_duration(p[key]["total_fmt"])}',
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
    np = d.get("now_playing")
    lines = [d["emoji_header"]]
    lines.append(f'Grade: {q["grade"]} 🎧  ({q["days_behind_fmt"]} days behind)')
    lines.append("")
    if np:
        where = f'{np["podcast"]} — ' if np["podcast"] else ""
        lines.append("▶️ NOW PLAYING")
        lines.append(f'"{np["title"]}"')
        lines.append(f'{where}{np["remaining_fmt"]} left')
        lines.append("")
    lines.append("📋 UNPLAYED QUEUE")
    if q["episodes"]:
        lines.append(f'{pluralize(q["count"], "episode", q["count_fmt"])} · {q["total_fmt"]}')
        lines.append(f'Oldest: {human_date(d, q["oldest_date"])} ({q["days_behind_fmt"]} days behind)')
    else:
        lines.append("Empty — you're all caught up!")
    return "\n".join(lines)

def build_email(d):
    q = d["queue"]
    p = d["played"]
    label = d.get("config", {}).get("label") or "Podcast Queue Report"
    subject = f'{label} — Grade {q["grade"]} ({fmt_days_decimal(q["days_behind"])} days behind)'
    lines = []
    lines.append(d["emoji_header"])
    lines.append(f'That papa is {q["days_behind_fmt"]} days behind the times — Grade: {q["grade"]} 🎧')
    lines.append("")
    now_playing = now_playing_sentence(d)
    if now_playing:
        lines.append(now_playing)
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

    now_playing_data = d.get("now_playing")

    def _is_now_playing(e):
        # Prefer matching by episode_url (stable identity from the store
        # link); title/podcast is only a fallback for episodes with no
        # store link populated.
        if not now_playing_data:
            return False
        if now_playing_data.get("episode_url") and e.get("episode_url"):
            return now_playing_data["episode_url"] == e["episode_url"]
        return (now_playing_data.get("title") == e.get("title")
                and now_playing_data.get("podcast") == e.get("podcast"))

    rows = ""
    for i, e in enumerate(q["episodes"]):
        pub = datetime.datetime.fromisoformat(e["pubdate"]).replace(tzinfo=datetime.timezone.utc).astimezone(local_tz(d))
        is_playing = _is_now_playing(e)
        if is_playing:
            badge = ' <span class="now-playing-badge">● NOW PLAYING</span>'
        elif i == 0:
            badge = ' <span class="up-next">▶ UP NEXT</span>'
        else:
            badge = ""
        title_html = html.escape(e["title"])
        if e.get("episode_url"):
            title_html = f'<a href="{html.escape(e["episode_url"])}" target="_blank">{title_html}</a>'
        pod_html = html.escape(e["podcast"])
        if e.get("podcast_url"):
            pod_html = f'<a href="{html.escape(e["podcast_url"])}" target="_blank">{pod_html}</a>'
        rows += f'''
        <tr class="{"up-next-row" if (i == 0 or is_playing) else ""}">
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

    np = d.get("now_playing")
    if np:
        title_html = html.escape(np["title"])
        if np.get("episode_url"):
            title_html = f'<a href="{html.escape(np["episode_url"])}" target="_blank">{title_html}</a>'
        podcast_html = html.escape(np["podcast"]) if np["podcast"] else ""
        if podcast_html and np.get("podcast_url"):
            podcast_html = f'<a href="{html.escape(np["podcast_url"])}" target="_blank">{podcast_html}</a>'
        now_playing_html = (
            '<div class="now-playing"><span class="dot"></span>'
            f'<span class="text">&#9654;&#65039; <b>Now playing:</b> {title_html}'
            + (f' &mdash; {podcast_html}' if podcast_html else "")
            + f' ({np["remaining_fmt"]} left)</span></div>'
        )
    else:
        now_playing_html = ""

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
  .now-playing {{ background: #ecfdf5; border: 1px solid #a7f3d0; border-radius: 12px; padding: 14px 20px; margin-bottom: 24px; display: flex; align-items: center; gap: 10px; }}
  .now-playing .dot {{ width: 10px; height: 10px; border-radius: 50%; background: #10b981; flex-shrink: 0; animation: pulse 1.6s ease-in-out infinite; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.35; }} }}
  .now-playing .text {{ font-size: 14px; color: #065f46; }}
  .now-playing .text b {{ color: #064e3b; }}
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
  .now-playing-badge {{ display: inline-block; background: #d1fae5; color: #047857; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; border-radius: 4px; padding: 1px 6px; margin-left: 6px; vertical-align: middle; }}
  .up-next-row {{ background: #f8fafc; }}
  .ep-date, .ep-dur {{ color: #475569; white-space: nowrap; }}
  .footer {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="container">
  <div class="emoji-strip">{d["emoji_header"]}</div>
  <div class="headline">That papa is {q["days_behind_fmt"]} days behind the times &mdash; Grade: <span class="grade-badge">{q["grade"]}</span> 🎧</div>

  {now_playing_html}

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
