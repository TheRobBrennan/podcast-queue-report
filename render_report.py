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

# Grades ride one continuous green -> yellow -> red ramp instead of a
# hand-picked color per grade, so a +/- step reads as "slightly worse than"
# rather than as its own category. Only the anchors below are chosen; every
# other grade is a linear RGB blend of its neighbors, which is why B lands
# olive-lime (between A's green and C's yellow) and D lands orange (between
# C's yellow and F's red).
GRADE_SCALE = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]
GRADE_ANCHORS = {
    0:  (0x15, 0x80, 0x3d),   # A+  deep green
    1:  (0x16, 0xa3, 0x4a),   # A   green
    7:  (0xea, 0xb3, 0x08),   # C   yellow
    10: (0xea, 0x58, 0x0c),   # D   orange
    12: (0xb9, 0x1c, 0x1c),   # F   red
}
GRADE_FALLBACK = (0x33, 0x41, 0x55)  # slate, for anything not on the scale


def grade_rgb(grade):
    if grade not in GRADE_SCALE:
        return GRADE_FALLBACK
    i = GRADE_SCALE.index(grade)
    if i in GRADE_ANCHORS:
        return GRADE_ANCHORS[i]
    stops = sorted(GRADE_ANCHORS)
    lo = max(s for s in stops if s < i)
    hi = min(s for s in stops if s > i)
    t = (i - lo) / (hi - lo)
    return tuple(
        round(GRADE_ANCHORS[lo][c] + t * (GRADE_ANCHORS[hi][c] - GRADE_ANCHORS[lo][c]))
        for c in range(3)
    )


def grade_hex(grade):
    return "#%02x%02x%02x" % grade_rgb(grade)


def grade_int(grade):
    r, g, b = grade_rgb(grade)
    return (r << 16) | (g << 8) | b


def grade_text_color(grade):
    """White text goes unreadable against the yellow/lime middle of the ramp,
    so derive the badge's text color from the badge's own luminance (WCAG
    relative luminance) instead of hardcoding white."""
    chans = [c / 255 for c in grade_rgb(grade)]
    lin = [((c + 0.055) / 1.055) ** 2.4 if c > 0.03928 else c / 12.92 for c in chans]
    lum = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
    return "#ffffff" if 1.05 / (lum + 0.05) >= 4.0 else "#1e293b"

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

def fmt_relative(d, iso):
    """Podcasts.app-style relative age: "13h ago", "1d ago", "Just now".
    Anything older than a week falls back to an absolute date, same as the
    app does — "6d ago" is legible, "43d ago" isn't. Measured against the
    report's own generated_at so every line in one report agrees."""
    then = datetime.datetime.fromisoformat(iso).replace(tzinfo=datetime.timezone.utc)
    now = datetime.datetime.fromisoformat(d["generated_at"]).replace(tzinfo=datetime.timezone.utc)
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "Just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 7 * 86400:
        return f"{int(seconds // 86400)}d ago"
    local = then.astimezone(local_tz(d))
    return local.strftime("%b %-d") if local.year == now.astimezone(local_tz(d)).year else local.strftime("%b %-d, %Y")

def pluralize(n, noun, n_fmt=None):
    disp = n_fmt if n_fmt is not None else n
    return f"{disp} {noun}" if n == 1 else f"{disp} {noun}s"

def headline(q):
    """The 'That papa is ...' opener, which reads differently when there's
    nothing left to catch up on."""
    if q["days_behind_amount"]:
        return f'That papa is {q["days_behind_amount"]} behind the times'
    return "That papa is right on time — current as of today"


def now_playing_sentence(d):
    """Just the episode detail — the section header is added by
    build_chat_summary so the same sentence stays reusable without a
    baked-in label."""
    np = d.get("now_playing")
    if not np:
        return None
    where = f' on {np["podcast"]}' if np["podcast"] else ""
    return f'"{np["title"]}"{where} — {np["remaining_fmt"]} left'

def up_next_sentence(d):
    q = d["queue"]
    if not q["episodes"]:
        return "The queue is empty — you're all caught up!"
    ep = q["episodes"][0]
    return (f'{pluralize(q["count"], "episode", q["count_fmt"])} in the queue · {q["total_fmt"]} total\n'
            f'"{ep["title"]}" from {fmt_relative(d, ep["pubdate"])} — {q["up_next_remaining_fmt"]} left to finish')

def build_chat_summary(d):
    q = d["queue"]
    p = d["played"]
    lines = []
    lines.append(d["emoji_header"])
    lines.append(f'{headline(q)} — Grade: {q["grade"]} 🎧')
    lines.append("")
    now_playing = now_playing_sentence(d)
    if now_playing:
        lines.append("▶️  NOW PLAYING")
        lines.append(now_playing)
        lines.append("")
    lines.append("📥  UP NEXT")
    lines.append(up_next_sentence(d))
    lines.append("")
    played_rows = [(key, label) for key, label in PLAYED_LABELS if key in p]
    if played_rows:
        lines.append("📊  PLAYED")
        label_width = max(len(label) for _, label in played_rows)
        for key, label in played_rows:
            stats = p[key]
            lines.append(f'  {label.ljust(label_width)}   {pluralize(stats["count"], "ep", stats["count_fmt"])}, {stats["total_fmt"]}')
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

    description = f'**Grade {q["grade"]}** — {q["days_behind_phrase"]} 🎧\n\n'
    np = d.get("now_playing")
    if np:
        title_link = _discord_link(f'"{np["title"]}"', np.get("episode_url"))
        where = f' — {_discord_link(np["podcast"], np.get("podcast_url"))}' if np["podcast"] else ""
        description += f'🟢 **Now playing:** {title_link}{where} ({np["remaining_fmt"]} left)\n\n'
    if q["episodes"]:
        ep = q["episodes"][0]
        title_link = _discord_link(f'"{ep["title"]}"', ep.get("episode_url"))
        where = f' — {_discord_link(ep["podcast"], ep.get("podcast_url"))}' if ep.get("podcast") else ""
        # Mirror the HTML report's episode table: always the publish age, plus
        # how much is left to finish when the episode has been started. Without
        # the second half a partially-played episode reads as a full-length
        # listen still ahead of you, when it may be minutes from done.
        detail = fmt_relative(d, ep["pubdate"])
        if ep.get("in_progress") and ep.get("remaining_fmt"):
            detail += f' · {ep["remaining_fmt"]} left'
        elif q.get("up_next_remaining_fmt"):
            detail += f' · {q["up_next_remaining_fmt"]}'
        description += f'**Up next:** {title_link}{where} ({detail})'
    else:
        description += "**Up next:** queue is empty — you're all caught up!"

    field_icons = {
        "queue": "📥", "since_last_run": "⏱️", "yesterday": "📆",
        "past_week": "🗓️", "past_month": "📅", "all_time": "⏳",
    }
    fields = [{
        "name": f'__{field_icons["queue"]} Unplayed__',
        "value": f'{pluralize(q["count"], "episode", q["count_fmt"])}\n{wrap_duration(q["total_fmt"])}',
        "inline": True,
    }]
    for key, label in PLAYED_LABELS:
        if key in p:
            fields.append({
                "name": f'__{field_icons[key]} {label}__',
                "value": f'{pluralize(p[key]["count"], "episode", p[key]["count_fmt"])}\n{wrap_duration(p[key]["total_fmt"])}',
                "inline": True,
            })

    embed = {
        "title": f'{d["emoji_header"]}',
        "description": description,
        "color": grade_int(q["grade"]),
        "fields": fields,
        "footer": {"text": "Podcast Queue Report"},
        "timestamp": d["generated_at"],
    }
    return {"embeds": [embed]}

def build_sms(d):
    q = d["queue"]
    np = d.get("now_playing")
    lines = [d["emoji_header"]]
    lines.append(f'Grade: {q["grade"]} 🎧  ({q["days_behind_phrase"]})')
    lines.append("")
    if np:
        where = f'{np["podcast"]} — ' if np["podcast"] else ""
        lines.append("▶️ NOW PLAYING")
        lines.append(f'"{np["title"]}"')
        lines.append(f'{where}{np["remaining_fmt"]} left')
        lines.append("")
    lines.append("📋 UNPLAYED")
    if q["episodes"]:
        lines.append(f'{pluralize(q["count"], "episode", q["count_fmt"])} · {q["total_fmt"]}')
        lines.append(f'Oldest: {fmt_relative(d, q["oldest_date"])} ({q["days_behind_phrase"]})')
    else:
        lines.append("Empty — you're all caught up!")
    return "\n".join(lines)

def build_email(d):
    q = d["queue"]
    label = d.get("config", {}).get("label") or "Podcast Queue Report"
    subject = f'{label} — Grade {q["grade"]} ({q["days_behind_phrase"]})'
    # Same experience as the standalone HTML report - no separate plain-text
    # summary to keep in sync as build_html grows new sections.
    return subject, build_html(d)

def build_html(d):
    q = d["queue"]
    p = d["played"]

    def duration_html(total_fmt):
        # fmt_duration joins "N\u00a0unit" pairs with plain (breakable)
        # spaces between pairs and a non-breaking space within each pair,
        # so this just lets the browser/client wrap it naturally instead of
        # forcing one unit per line - that made card heights wildly
        # inconsistent (1 line vs 5 lines) when two cards sit side by side
        # in the same table row.
        return html.escape(total_fmt)

    def stat_card(label, stats):
        noun = "episode" if stats["count"] == 1 else "episodes"
        return f'''
        <td width="50%" valign="top" style="padding:0 8px 16px 0;">
          <div style="background:#ffffff;border-radius:12px;padding:22px 20px;box-shadow:0 1px 3px rgba(0,0,0,0.08);height:100%;box-sizing:border-box;">
            <div style="font-size:11px;font-weight:600;text-transform:uppercase;color:#94a3b8;letter-spacing:0.6px;margin-bottom:8px;">{html.escape(label)}</div>
            <div style="margin-bottom:8px;">
              <span style="font-size:28px;font-weight:700;line-height:1;">{stats["count_fmt"]}</span>
              <span style="font-size:13px;font-weight:600;color:#94a3b8;">{noun}</span>
            </div>
            <div style="font-size:13px;color:#64748b;line-height:1.7;margin-top:4px;">{duration_html(stats["total_fmt"])}</div>
          </div>
        </td>'''

    card_cells = [stat_card(label, p[key]) for key, label in PLAYED_LABELS if key in p]
    card_rows = ""
    for i in range(0, len(card_cells), 2):
        pair = card_cells[i:i + 2]
        if len(pair) == 1:
            pair.append('<td width="50%" style="padding:0 8px 16px 0;"></td>')
        card_rows += f"<tr>{''.join(pair)}</tr>"

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
            badge = ' <span style="display:inline-block;background:#d1fae5;color:#047857;font-size:10px;font-weight:700;letter-spacing:0.5px;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;">● NOW PLAYING</span>'
        elif i == 0:
            badge = ' <span style="display:inline-block;background:#dbeafe;color:#1d4ed8;font-size:10px;font-weight:700;letter-spacing:0.5px;border-radius:4px;padding:1px 6px;margin-left:6px;vertical-align:middle;">▶ UP NEXT</span>'
        else:
            badge = ""
        title_html = html.escape(e["title"])
        if e.get("episode_url"):
            title_html = f'<a href="{html.escape(e["episode_url"])}" target="_blank" style="color:#1e293b;text-decoration:none;">{title_html}</a>'
        pod_html = html.escape(e["podcast"])
        if e.get("podcast_url"):
            pod_html = f'<a href="{html.escape(e["podcast_url"])}" target="_blank" style="color:#64748b;text-decoration:none;">{pod_html}</a>'
        row_bg = "background:#f8fafc;" if (i == 0 or is_playing) else ""
        remaining_html = (
            f'<div style="color:#2563eb;font-size:12px;margin-top:2px;">{e["remaining_fmt"]} left</div>'
            if e.get("in_progress") and e.get("remaining_fmt") else ""
        )
        rows += f'''
        <tr style="{row_bg}">
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;vertical-align:top;font-size:14px;font-weight:600;">{title_html}{badge}<div style="font-weight:400;color:#64748b;font-size:12px;margin-top:2px;">{pod_html}</div></td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;vertical-align:top;font-size:14px;color:#475569;white-space:nowrap;" title="{pub.strftime("%a %b %-d, %-I:%M%p %Z")}">{fmt_relative(d, e["pubdate"])}</td>
          <td style="padding:12px 16px;border-bottom:1px solid #f1f5f9;vertical-align:top;font-size:14px;color:#475569;white-space:nowrap;">{e["duration_fmt"]}{remaining_html}</td>
        </tr>'''

    grade_color = grade_hex(q["grade"])
    grade_text = grade_text_color(q["grade"])

    np = d.get("now_playing")
    if np:
        title_html = html.escape(np["title"])
        if np.get("episode_url"):
            title_html = f'<a href="{html.escape(np["episode_url"])}" target="_blank">{title_html}</a>'
        podcast_html = html.escape(np["podcast"]) if np["podcast"] else ""
        if podcast_html and np.get("podcast_url"):
            podcast_html = f'<a href="{html.escape(np["podcast_url"])}" target="_blank">{podcast_html}</a>'
        now_playing_html = (
            '<div style="background:#ecfdf5;border:1px solid #a7f3d0;border-radius:12px;'
            'padding:14px 20px;margin-bottom:32px;">'
            '<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            'background:#10b981;margin-right:10px;">&nbsp;</span>'
            f'<span style="font-size:14px;color:#065f46;">&#9654;&#65039; '
            f'<b style="color:#064e3b;">Now playing:</b> {title_html}'
            + (f' &mdash; {podcast_html}' if podcast_html else "")
            + f' ({np["remaining_fmt"]} left)</span></div>'
        )
    else:
        now_playing_html = ""

    # No oldest episode at all means the queue is empty — there's nothing to
    # date-stamp, so say so instead of printing an "Oldest:" line.
    if q["oldest_date"]:
        oldest_html = (f'Oldest: <span title="{human_date(d, q["oldest_date"])}">'
                       f'{fmt_relative(d, q["oldest_date"])}</span> ({q["days_behind_phrase"]})')
    else:
        oldest_html = "Empty &mdash; you&rsquo;re all caught up!"

    return f'''<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Podcast Queue Report</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f8fafc;margin:0;padding:32px;color:#1e293b;">
<div style="max-width:720px;margin:0 auto;">
  <div style="font-size:28px;letter-spacing:2px;text-align:center;margin-bottom:8px;line-height:1.4;">{d["emoji_header"]}</div>
  <div style="text-align:center;font-size:20px;font-weight:600;margin-bottom:36px;">{headline(q)} &mdash; <span style="white-space:nowrap;">Grade: <span style="display:inline-block;background:{grade_color};color:{grade_text};border-radius:8px;padding:2px 12px;font-weight:700;">{q["grade"]}</span> 🎧</span></div>

  {now_playing_html}

  <div style="background:#ffffff;border-radius:12px;padding:20px 24px;margin-bottom:32px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
    <h2 style="margin:0 0 4px;font-size:15px;text-transform:uppercase;letter-spacing:0.5px;color:#64748b;">Unplayed</h2>
    <div style="font-size:32px;font-weight:700;">{q["count_fmt"]} episodes &middot; {q["total_fmt"]}</div>
    <div style="color:#64748b;font-size:14px;margin-top:4px;">{oldest_html}</div>
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:20px;">
    {card_rows}
  </table>

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:12px;box-shadow:0 1px 3px rgba(0,0,0,0.08);">
    <tr>
      <th style="text-align:left;font-size:12px;text-transform:uppercase;color:#94a3b8;padding:12px 16px;border-bottom:1px solid #e2e8f0;">Episode</th>
      <th style="text-align:left;font-size:12px;text-transform:uppercase;color:#94a3b8;padding:12px 16px;border-bottom:1px solid #e2e8f0;">Published</th>
      <th style="text-align:left;font-size:12px;text-transform:uppercase;color:#94a3b8;padding:12px 16px;border-bottom:1px solid #e2e8f0;">Length</th>
    </tr>
    {rows}
  </table>

  <div style="text-align:center;color:#94a3b8;font-size:12px;margin-top:32px;">Generated {human_date(d, d["generated_at"])}</div>
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
