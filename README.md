# Podcast Queue Report

> **Reference implementation only.** This repo was built and tested against
> one specific Mac (with Apple Podcasts installed and a particular library
> layout) inside an AI coding session. It is **not verified or validated to
> run on other macOS systems** — Apple's Podcasts database schema is
> undocumented, private, and can change without notice between app/OS
> versions. Treat this as a working example to read, learn from, and adapt,
> not a plug-and-play tool. See "Known caveats" below.

Reads a local Apple Podcasts library (SQLite) and generates a summary of the
"Latest Episodes" unplayed queue plus listening stats, with a letter grade
for how many days behind the oldest unplayed episode is.

## Setup

```bash
git clone <this-repo-url> "podcast-queue-report"
cd podcast-queue-report
cp .env.example .env
# edit .env with your own timezone, email, phone, etc.
python3 podcast_summary.py > /tmp/run.json
python3 render_report.py /tmp/run.json chat
```

No external Python dependencies are required (standard library only,
including a minimal built-in `.env` loader).

### Using this as a Claude Code skill

To let Claude (via Claude Code or Claude Cowork) run this on your behalf:

1. Clone this repo somewhere on disk, and fill in `.env` as above.
2. Point Claude at it — e.g. ask it to "read `SKILL.md` in this repo and use
   it to run my podcast queue report," or copy `SKILL.md` into your own
   Claude skills directory (`~/.claude/skills/podcast-queue-report/` for
   Claude Code, or save it as a Cowork skill) and adjust the file paths
   inside it to point at wherever you cloned this repo.
3. Claude will need read access to your Apple Podcasts library at
   `~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/`
   and to this repo's folder.

## Files

- `podcast_summary.py` — queries the Podcasts SQLite database, computes the
  unplayed queue (with a reverse-engineered heuristic — see caveats below),
  listening stats for several time windows, episode/podcast links, and
  writes JSON to stdout. Durations are formatted human-friendly (e.g. "1 day
  2 hrs 38 mins 12 seconds") and episode/podcast titles link to their Apple
  Podcasts pages. Reads configuration from environment variables / `.env`.
- `render_report.py` — renders that JSON into a chat summary, SMS text, email
  (subject + body), or a styled standalone HTML report.
- `reports/podcast_report.html` — the most recently generated HTML report.
- `.env.example` — template for the environment variables below. Copy to
  `.env` (gitignored) and fill in your own values.
- `.podcast_skill_state.json` (gitignored, local only) — tracks the last run
  timestamp, used for the "since last check" played-stats window.
- `SKILL.md` — self-contained instructions for an AI agent (e.g. Claude) to
  run this report and, with explicit user confirmation, deliver it by email
  or text.

## Environment variables

| Variable | Purpose | Default if unset |
|---|---|---|
| `PODCASTS_DB_PATH` | Path to `MTLibrary.sqlite` | Auto-detects the standard macOS location |
| `REPORT_TIMEZONE` | IANA timezone for date/window boundaries | `UTC` |
| `REPORT_SIGNOFF_NAME` | Name used in the email sign-off | (omitted if unset) |
| `REPORT_EMAIL` | Delivery email address | (none) |
| `REPORT_PHONE` | Delivery phone number | (none) |
| `REPORT_LABEL` | Prefix used to build the email subject line | `Podcast Queue Report` |
| `REPORT_EMAIL_CLIENT` | Which email client to use (informational, read by the Claude skill) | (none) |

## Usage

```bash
python3 podcast_summary.py > /tmp/run.json
python3 render_report.py /tmp/run.json chat   # plain-text chat summary
python3 render_report.py /tmp/run.json sms    # short SMS text
python3 render_report.py /tmp/run.json email  # SUBJECT: ... / body
python3 render_report.py /tmp/run.json html > reports/podcast_report.html
```

## Grading scale

Grade is based on how many days behind the oldest unplayed episode's publish
date is versus now (in `REPORT_TIMEZONE`).

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

Delivery (email/text) is handled by an AI agent driving the Mac's UI (Mail,
Outlook, Messages, etc.), not by this code — see `SKILL.md`. It always
requires explicit user go-ahead before sending anything, even on an
automated schedule.

## Known caveats

- The unplayed-queue detection (`ZUNPLAYEDTAB=1 AND ZPLAYSTATE IN (1,2)` for
  subscribed podcasts, taking the contiguous run back to the last >20h gap)
  was reverse-engineered from Apple's undocumented internal schema by
  comparing SQL query output against a screenshot of the actual Podcasts.app
  "Latest Episodes" view. It is a heuristic, not documented Apple behavior —
  it could break on a different library, macOS version, or Podcasts app
  update.
- "Played" stats count any episode with a `ZLASTDATEPLAYED` timestamp, not
  necessarily ones fully finished — there's no reliable "completed" flag in
  the data, so totals (especially all-time) likely overcount true full
  listens.
- Episode/podcast links point to Apple Podcasts (`podcasts.apple.com`),
  built from each show's store page plus the episode's store track ID —
  both are populated inconsistently depending on the podcast's source feed.
- This has only ever been run against one person's library on one Mac.
  Column names, value semantics, and even table structure could differ on
  other setups (different macOS/Podcasts versions, iCloud sync state,
  library size, etc.).
