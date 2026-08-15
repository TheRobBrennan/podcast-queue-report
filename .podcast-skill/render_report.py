import json, sys, datetime, html
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("America/Los_Angeles")

def human_date(iso):
    # Stored timestamps are naive UTC; convert to local time for display.
    d = datetime.datetime.fromisoformat(iso).replace(tzinfo=datetime.timezone.utc)
    d_local = d.astimezone(LOCAL_TZ)
    return d_local.strftime("%a %b %-d, %-I:%M%p %Z")

def build_chat_summary(d):
    q = d["queue"]
    p = d["played"]
    days = q["days_behind"]
    days_disp = f"{days:.1f}".rstrip("0").rstrip(".") if days else "0"
    lines = []
    lines.append(d["emoji_header"])
    lines.append(f'That papa is {days_disp} days behind the times — Grade: {q["grade"]} 🎧')
    lines.append("")
    lines.append(f'Unplayed queue: {q["count_fmt"]} episodes, {q["total_hm"]} total. Oldest (up next): {human_date(q["oldest_date"])} ({days_disp}d behind).')
    lines.append("")
    lines.append("Played:")
    if "since_last_run" in p:
        lines.append(f'  Since last check: {p["since_last_run"]["count_fmt"]} eps, {p["since_last_run"]["total_hm"]}')
    lines.append(f'  Yesterday: {p["yesterday"]["count_fmt"]} eps, {p["yesterday"]["total_hm"]}')
    lines.append(f'  Past week: {p["past_week"]["count_fmt"]} eps, {p["past_week"]["total_hm"]}')
    lines.append(f'  Past month: {p["past_month"]["count_fmt"]} eps, {p["past_month"]["total_hm"]}')
    lines.append(f'  All time: {p["all_time"]["count_fmt"]} eps, {p["all_time"]["total_hm"]}')
    return "\n".join(lines)

def build_sms(d):
    q = d["queue"]
    p = d["played"]
    days = q["days_behind"]
    days_disp = f"{days:.1f}".rstrip("0").rstrip(".") if days else "0"
    since = ""
    if "since_last_run" in p:
        since = f' Since last check: {p["since_last_run"]["count_fmt"]} played.'
    return (f'🎧 Queue: {q["count_fmt"]} eps ({q["total_hm"]}), {days_disp}d behind — Grade {q["grade"]}. '
            f'Yesterday: {p["yesterday"]["count_fmt"]} played.{since}')

def build_html(d):
    q = d["queue"]
    p = d["played"]
    days = q["days_behind"]
    days_disp = f"{days:.1f}".rstrip("0").rstrip(".") if days else "0"

    def stat_card(label, stats):
        return f'''
        <div class="card">
          <div class="card-label">{html.escape(label)}</div>
          <div class="card-count">{stats["count_fmt"]}</div>
          <div class="card-sub">episodes &middot; {stats["total_hm"]}</div>
        </div>'''

    played_cards = ""
    if "since_last_run" in p:
        played_cards += stat_card("Since last check", p["since_last_run"])
    played_cards += stat_card("Yesterday", p["yesterday"])
    played_cards += stat_card("Past week", p["past_week"])
    played_cards += stat_card("Past month", p["past_month"])
    played_cards += stat_card("All time", p["all_time"])

    rows = ""
    for i, e in enumerate(q["episodes"]):
        pub = datetime.datetime.fromisoformat(e["pubdate"]).replace(tzinfo=datetime.timezone.utc).astimezone(LOCAL_TZ)
        badge = ' <span class="up-next">▶ UP NEXT</span>' if i == 0 else ""
        rows += f'''
        <tr class="{"up-next-row" if i == 0 else ""}">
          <td class="ep-title">{html.escape(e["title"])}{badge}<div class="ep-pod">{html.escape(e["podcast"])}</div></td>
          <td class="ep-date">{pub.strftime("%a %b %-d")}</td>
          <td class="ep-dur">{e["duration_hm"]}</td>
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
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 28px; }}
  .card {{ background: white; border-radius: 12px; padding: 16px 18px; flex: 1 1 130px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  .card-label {{ font-size: 12px; text-transform: uppercase; color: #94a3b8; letter-spacing: 0.5px; margin-bottom: 6px; }}
  .card-count {{ font-size: 26px; font-weight: 700; }}
  .card-sub {{ font-size: 13px; color: #64748b; margin-top: 2px; }}
  table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 12px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th {{ text-align: left; font-size: 12px; text-transform: uppercase; color: #94a3b8; padding: 12px 16px; border-bottom: 1px solid #e2e8f0; }}
  td {{ padding: 12px 16px; border-bottom: 1px solid #f1f5f9; vertical-align: top; font-size: 14px; }}
  .ep-title {{ font-weight: 600; }}
  .ep-pod {{ font-weight: 400; color: #64748b; font-size: 12px; margin-top: 2px; }}
  .up-next {{ display: inline-block; background: #dbeafe; color: #1d4ed8; font-size: 10px; font-weight: 700; letter-spacing: 0.5px; border-radius: 4px; padding: 1px 6px; margin-left: 6px; vertical-align: middle; }}
  .up-next-row {{ background: #f8fafc; }}
  .ep-date, .ep-dur {{ color: #475569; white-space: nowrap; }}
  .footer {{ text-align: center; color: #94a3b8; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<div class="container">
  <div class="emoji-strip">{d["emoji_header"]}</div>
  <div class="headline">That papa is {days_disp} days behind the times &mdash; Grade: <span class="grade-badge">{q["grade"]}</span> 🎧</div>

  <div class="queue-summary">
    <h2>Unplayed Queue</h2>
    <div class="big">{q["count"]} episodes &middot; {q["total_hm"]}</div>
    <div class="sub">Oldest: {human_date(q["oldest_date"])} ({days_disp} days behind)</div>
  </div>

  <div class="cards">
    {played_cards}
  </div>

  <table>
    <tr><th>Episode</th><th>Published</th><th>Length</th></tr>
    {rows}
  </table>

  <div class="footer">Generated {human_date(d["generated_at"])}</div>
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
    elif mode == "html":
        print(build_html(data))
