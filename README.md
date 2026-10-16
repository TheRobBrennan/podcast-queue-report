# Podcast Queue Report

> **Reference implementation only.** This repo was built and tested against
> one specific Mac (with Apple Podcasts installed and a particular library
> layout) inside an AI coding session. It is **not verified or validated to
> run on other macOS systems** — Apple's Podcasts database schema is
> undocumented, private, and can change without notice between app/OS
> versions. Treat this as a working example to read, learn from, and adapt,
> not a plug-and-play tool. See "Known caveats" below.

Reads a local Apple Podcasts library (SQLite) and generates a summary of
your genuinely unplayed episode backlog plus listening stats, with a
letter grade for how many days behind the oldest unplayed episode is.

## In action

Apple Podcasts' own "Latest Episodes" view — a recency feed, not an
unplayed filter (see "Known caveats" below for why this repo's queue
doesn't try to match it):

![Apple Podcasts Latest Episodes view](assets/podcasts-app-latest-episodes.png)

The generated HTML report:

![HTML report demo](assets/html-report.gif)

Grades are color-coded on one continuous green &rarr; yellow &rarr; red
ramp. Only five anchors are chosen — A+ deep green, A green, C yellow, D
orange, F red — and every other grade is a linear RGB blend of its
neighbors, so a +/- step reads as "slightly worse than" rather than as its
own category:

![Grade badge colors](assets/grade-colors.png)

The same ramp in the report headline, at three points on the scale:

![Grade A headline](assets/grade-headline-a.png)
![Grade C headline](assets/grade-headline-c.png)
![Grade F headline](assets/grade-headline-f.png)

Badge text color isn't fixed either — it's derived from each badge's own
WCAG relative luminance, since white text goes unreadable across the
yellow/lime middle of the ramp. Regenerate the swatch sheet after any
palette change with `python3 scripts/make_grade_swatches.py`, which reads
the colors from `render_report.py` itself rather than re-listing them.

The SMS text, as received:

![SMS report](assets/sms-report.png)

The Discord embed, as posted:

![Discord report](assets/discord-report.png)

## Setup

```bash
git clone <this-repo-url> "podcast-queue-report"
cd podcast-queue-report
make setup          # copies .env.example -> .env
# now edit .env with your own timezone, email, phone, etc.
make chat            # first run — prints the chat summary
```

No external Python dependencies are required (standard library only,
including a minimal built-in `.env` loader). `make` is preinstalled on
macOS (via Xcode Command Line Tools) — if `make setup` says "command not
found," run `xcode-select --install` first, or just do the two steps by
hand: `cp .env.example .env`, then edit it.

Optional: install [`nowplaying-cli`](https://github.com/ungive/nowplaying-cli)
(`brew install nowplaying-cli`) to have reports call out an episode you're
actively playing right now ("Now playing"). Without it, every report just
skips that section — nothing else depends on it.

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

## Quick commands (Makefile)

This is the recommended way to run everything — for both humans and an AI
agent working in this repo. Run `make help` any time for the full list.

| Command | What it does |
|---|---|
| `make setup` | First-time setup: copies `.env.example` to `.env` |
| `make run` | Queries the Podcasts DB once, writes `/tmp/podcast_run.json` |
| `make chat` | Prints the chat summary (preview only, nothing sent) |
| `make html` | Regenerates `reports/podcast_report.html` |
| `make email` | Emails the report via Outlook (needs `REPORT_EMAIL` in `.env`) |
| `make sms` | Texts the report via Messages.app (needs `REPORT_PHONE` in `.env`) |
| `make discord` | Posts the chat summary to Discord via webhook (needs `DISCORD_WEBHOOK_URL` in `.env`) |
| `make cron` | Runs the query once, then discord + email (what the launchd agent runs) |
| `make open` | Regenerates the HTML report and opens it in your default browser (macOS `open`) |
| `make all` | Runs the query once, then chat + html + email + sms + discord (each send skips cleanly if unconfigured) |
| `make snapshot LABEL=morning` | Queries once, records the snapshot in the flow history, posts to Discord (`LABEL` is `morning`, `dash-start`, `dash-end`, `evening`, or `manual`; `evening` also emails the daily flow) |
| `make flow` | Prints today's daily flow from the snapshot history |
| `make week` | Prints this week's flow roll-up |
| `make tick` | What launchd runs every hour: takes a fallback snapshot only if one is due (`FLAGS=--dry-run` to just look) |
| `make unlock` | Clears stale git lock files (see `scripts/git_unlock.py`) |
| `make commit MSG='...'` | Unlocks, stages everything, commits |
| `make clean` | Removes the scratch `/tmp/podcast_run.json` file |

`npm start` is a shortcut for `make all` followed by opening the fresh HTML
report in your browser, with a `time` around the whole thing.

`chat`, `email`, `sms`, `html`, and `discord` all depend on `run`. Calling
any one of them alone re-queries the database fresh; `make all` only
queries once and reuses that same snapshot for everything it does
(important, since each query also advances the "since last check" stats
window — you don't want `make all` to silently zero that out by running
the query five times in a row).

`make open` uses the standard macOS `open` command, so it launches your
actual default browser — but only when run from a real shell on the Mac
(a Terminal, or Claude Code with direct shell access). It will silently do
nothing useful from inside Claude Cowork's sandboxed bash, since that shell
is an isolated Linux container with no path to your real screen. In that
environment, an AI agent running this skill should present the HTML file to
you to open yourself, or use OS-level screen control — see `SKILL.md`.

### Running the underlying scripts directly

The Makefile is a thin wrapper — you can always call the Python directly:

```bash
python3 podcast_summary.py > /tmp/run.json
python3 render_report.py /tmp/run.json chat   # plain-text chat summary
python3 render_report.py /tmp/run.json sms    # short SMS text
python3 render_report.py /tmp/run.json email  # SUBJECT: ... / styled HTML body
python3 render_report.py /tmp/run.json html > reports/podcast_report.html
```

## Files

- `podcast_summary.py` — queries the Podcasts SQLite database, computes the
  unplayed queue (with a reverse-engineered heuristic — see caveats below),
  listening stats for several time windows, episode/podcast links, and
  writes JSON to stdout. Durations are formatted human-friendly (e.g. "1 day
  2 hrs 38 mins 12 seconds") and episode/podcast titles link to their Apple
  Podcasts pages. Reads configuration from environment variables / `.env`.
- `render_report.py` — renders that JSON into a chat summary, SMS text, email
  (subject + the same styled HTML as the html report), or a styled
  standalone HTML report.
- `reports/podcast_report.html` — the most recently generated HTML report.
- `Makefile` — convenience commands wrapping the two scripts above; see
  "Quick commands" below.
- `scripts/git_unlock.py` — clears stale git lock files on filesystems that
  block deletes (see its own docstring for the full story).
- `scripts/make_grade_swatches.py` — regenerates `assets/grade-colors.png`,
  the grade color swatch sheet in "In action" above, straight from
  `render_report.py`'s own palette functions.
- `.env.example` — template for the environment variables below. Copy to
  `.env` (gitignored) and fill in your own values.
- `.podcast_skill_state.json` (gitignored, local only) — tracks the last run
  timestamp, used for the "since last check" played-stats window.
- `SKILL.md` — self-contained instructions for an AI agent (e.g. Claude) to
  run this report and automatically deliver it by email, text, and/or
  Discord, gated by which `.env` variables are configured.
- `scripts/launchd_run.sh` — what the scheduled launchd agent executes: a
  launchd-safe `PATH`, an `.env` check, a minimum-gap guard, then
  `make cron` (Discord + email). Supports `DRY_RUN=1` to exercise everything but sending.
- `deploy/ai.sploosh.podcast-queue-report.plist` — the launchd agent that
  runs the report on a schedule with no AI session involved. See
  `docs/LOCAL-SCHEDULING.md`.
- `docs/LOCAL-SCHEDULING.md` — how the scheduled local runs are set up,
  verified, and turned off.

## Environment variables

| Variable | Purpose | Default if unset |
|---|---|---|
| `PODCASTS_DB_PATH` | Path to `MTLibrary.sqlite` | Auto-detects the standard macOS location |
| `REPORT_TIMEZONE` | IANA timezone for date/window boundaries | the Mac's system timezone |
| `REPORT_SIGNOFF_NAME` | Name used in the email sign-off | (omitted if unset) |
| `REPORT_EMAIL` | Delivery email address - `make email` (Outlook) skips if unset | (none) |
| `REPORT_PHONE` | Delivery phone number - `make sms` (Messages.app) skips if unset | (none) |
| `REPORT_LABEL` | Prefix used to build the email subject line | `Podcast Queue Report` |
| `DISCORD_WEBHOOK_URL` | Discord webhook - `make discord` skips if unset | (none) |

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

`make email`, `make sms`, and `make discord` each send for real -
`scripts/send_email.py` drives Outlook, `scripts/send_sms.py` drives
Messages.app (both via AppleScript, macOS only), and
`scripts/post_discord.py` posts to a webhook. Each skips cleanly with an
explanatory message if its `.env` destination (`REPORT_EMAIL`,
`REPORT_PHONE`, `DISCORD_WEBHOOK_URL`) is blank - that's the on/off switch
for each channel, there's no per-run confirmation prompt. `make all` /
`npm start` run all of it from one query. See `SKILL.md` for how an AI
agent should drive this.

## Daily flow

A fixed report every two hours says little: a Dash, a down day at the house,
or an early morning each move the queue for reasons the clock does not know
about. So instead of reporting by the clock, the report is built from a few
**snapshots tied to the day**, each one a normal single query:

| Snapshot | Taken when |
|---|---|
| `morning` | the day starts (`/start-day`), or an 11:00am-5:00pm fallback |
| `dash-start`, `dash-end` | a Dash begins and ends (the `/doordash` skill) |
| `evening` | the day closes (`/close-day`), or a 10:00pm fallback |

Every snapshot is appended to `.podcast_history.jsonl` (gitignored): the
queue size and length, how far behind, and what was played since the previous
snapshot. From that history `make flow` and the evening email report, for the
day:

- **Listened** - total played, from the report's own "since last check" figure
  summed over the day's snapshots.
- **Queue and behind** - start of day to end of day.
- **New arrivals (estimated)** - the change in queue length plus what was
  played, i.e. how much new content landed in the queue.
- **Dash windows** - for each `dash-start` to `dash-end` pair, what was
  listened to and how the queue and the behind figure moved.
- **Quietest stretch** - the longest run of snapshots with nothing played,
  when it is at least three hours.

`make week` adds the roll-up (total, daily average, best day, queue and
behind movement); the Sunday evening snapshot emails it. A "day" runs 4:00am
to 4:00am local time.

Between those, the hourly tick takes a quiet **pulse** reading about every two
hours (6am to 11pm, while the Mac is awake): history only, no Discord post, no
email. That keeps readings close to whatever you were doing, so a Dash you
announce **after the fact** still has readings near its real times:

```bash
make tag-dash START=10:11am END=6:18pm                 # today
make tag-dash DAY=2026-10-15 START=10:11am END=6:18pm  # another day
```

`tag-dash` labels the nearest ordinary readings (within two hours) as the Dash
start and end, marked "≈" in the report; it never overwrites a morning,
evening, or live Dash reading, and tags nothing if no reading is close enough.

Run `make snapshot LABEL=manual` any time for an extra data point. See
**[docs/LOCAL-SCHEDULING.md](docs/LOCAL-SCHEDULING.md)** for how the hourly
launchd tick decides when a fallback is due.

## Scheduled runs

On macOS a launchd user agent fires at :06 every hour and runs
`flow.py tick`, which takes a morning or evening fallback snapshot only when
one is due and the real trigger has not already fired. No AI session in the
loop, and missed fires coalesce into one instead of piling up after the lid
opens. See **[docs/LOCAL-SCHEDULING.md](docs/LOCAL-SCHEDULING.md)**.

## Known caveats

- The unplayed-queue detection (`get_unplayed_queue()` in
  `podcast_summary.py`: real played state, `ZPLAYSTATE != 2`, over a
  rolling `UNPLAYED_QUEUE_WINDOW_DAYS` window — 21 days as of this
  writing) was reverse-engineered from Apple's undocumented internal
  schema. It does **not** try to match Apple's own "Latest Episodes"
  view in Podcasts.app — that view turned out to be a plain recency
  feed with no played/unplayed filtering at all (confirmed 2026-09-10:
  a fully-played episode rendered there identically to genuinely
  unplayed ones), so matching it was never going to produce a correct
  "unplayed" report. This is a heuristic over Apple's undocumented
  schema, not documented behavior — it could break on a different
  library, macOS version, or Podcasts app update, and the window value
  itself was calibrated by direct observation (scrolling Rob's own
  Latest Episodes list to its oldest row), not derived from any
  readable setting. See the long numbered history in
  `get_unplayed_queue()`'s docstring for every wrong turn this took on
  the way here. If your queue ever looks off, re-verify the same way:
  a fresh screenshot of the real app, not another guess.
- "Played" stats count any episode with a `ZLASTDATEPLAYED` timestamp, not
  necessarily ones fully finished — there's no reliable "completed" flag in
  the data, so totals (especially all-time) likely overcount true full
  listens.
- "Now playing" detection relies on macOS's system-wide Now Playing info
  (via `nowplaying-cli`), not the podcast library — it only reflects
  Podcasts.app being the active player with a nonzero playback rate at
  the moment the report runs. It won't show anything if Podcasts is
  paused, backgrounded behind another app that's also using Now Playing,
  or `nowplaying-cli` isn't installed.
- Episode/podcast links point to Apple Podcasts (`podcasts.apple.com`),
  built from each show's store page plus the episode's store track ID —
  both are populated inconsistently depending on the podcast's source feed.
- This has only ever been run against one person's library on one Mac.
  Column names, value semantics, and even table structure could differ on
  other setups (different macOS/Podcasts versions, iCloud sync state,
  library size, etc.).
- **The `NOW PLAYING` episode artwork renders correctly in Apple Mail but
  looked cramped/oddly-scaled in Outlook for Mac** (observed 2026-09-14).
  The HTML is standards-correct - the source artwork is a clean 300x300
  square, and it measures out as a clean, undistorted 48x48 box in a
  Chromium render (verified with `getBoundingClientRect`), and it renders
  fine in Apple Mail on the same machine. Only Outlook for Mac's own HTML
  rendering shows the problem. Since `make email` sends through Outlook
  (see "Delivery" above), this is the client Rob actually reads the report
  in day to day. Not fixed - if you pick this back up, don't assume it's
  a CSS spacing/gap issue (that was tried and ruled out); get an actual
  Outlook-vs-Apple-Mail side-by-side screenshot first before changing
  anything, since headless Chrome cannot reproduce Outlook's renderer at
  all.
