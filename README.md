# Podcast Queue Report

Reads Rob's live Apple Podcasts library (`~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite`)
and generates a summary of the "Latest Episodes" unplayed queue plus listening
stats, with a letter grade for how many days behind the oldest unplayed
episode is.

## Files

- `podcast_summary.py` — queries the Podcasts SQLite database, computes the
  unplayed queue (with a reverse-engineered heuristic — see caveats below),
  listening stats for several time windows, episode/podcast links, and
  writes JSON to stdout. Durations are formatted human-friendly (e.g. "1 day
  2 hrs 38 mins 12 seconds") and episode/podcast titles link to their Apple
  Podcasts pages.
- `render_report.py` — renders that JSON into a chat summary, SMS text, email
  (subject + body), or a styled standalone HTML report.
- `reports/podcast_report.html` — the most recently generated HTML report.
- `.podcast_skill_state.json` (gitignored, local only) — tracks the last run
  timestamp, used for the "since last check" played-stats window.

## Usage

```
python3 podcast_summary.py > /tmp/run.json
python3 render_report.py /tmp/run.json chat   # plain-text chat summary
python3 render_report.py /tmp/run.json sms    # short SMS text
python3 render_report.py /tmp/run.json email  # SUBJECT: ... / body
python3 render_report.py /tmp/run.json html > reports/podcast_report.html
```

## Grading scale

Grade is based on how many days behind the oldest unplayed episode's publish
date is versus now (Pacific time).

| Days behind      | Grade |
|-------------------|:-----:|
| Queue is empty     | A+    |
| 0 days             | A     |
| 1 day               | A-    |
| 2 days              | B+    |
| 3 days              | B     |
| 4 days              | B-    |
| 5 – 7 days          | C+ / C / C- |
| 8 – 10 days         | D+ / D / D- |
| 11+ days            | F     |

## Delivery

Email goes out via Microsoft Outlook (not Mail app) to rob@sploosh.ai, with
the HTML report attached. Text goes to (360) 531-3830 via Messages — NOT
(206) 334-8483, which is an unused T-Mobile DIGITS line. Both require
explicit go-ahead each time; nothing sends automatically, even on the daily
7am schedule.

## Known caveats

- The unplayed-queue detection (`ZUNPLAYEDTAB=1 AND ZPLAYSTATE IN (1,2)` for
  subscribed podcasts, taking the contiguous run back to the last >20h gap)
  was reverse-engineered from Apple's undocumented internal schema and
  validated against Rob's actual on-screen "Latest Episodes" list on
  2026-08-15. It could break if Apple changes the app or schema.
- "Played" stats count any episode with a `ZLASTDATEPLAYED` timestamp, not
  necessarily ones fully finished — there's no reliable "completed" flag in
  the data, so totals (especially all-time) likely overcount true full
  listens.
- Day/week boundaries use Pacific time (`America/Los_Angeles`).
- Episode/podcast links point to Apple Podcasts (`podcasts.apple.com`),
  built from each show's store page plus the episode's store track ID.
