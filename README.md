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

## In action

The source data — Apple Podcasts' own "Latest Episodes" unplayed queue view:

![Apple Podcasts Latest Episodes view](assets/podcasts-app-latest-episodes.png)

The generated HTML report:

![HTML report demo](assets/html-report-demo.gif)

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
| `make chat` | Prints the chat summary |
| `make sms` | Prints the SMS text |
| `make email` | Prints the email `SUBJECT:` + body |
| `make html` | Regenerates `reports/podcast_report.html` |
| `make discord` | Posts the chat summary to Discord via webhook (needs `DISCORD_WEBHOOK_URL` in `.env`) |
| `make open` | Regenerates the HTML report and opens it in your default browser (macOS `open`) |
| `make all` | Runs the query once, then renders chat + sms + email + html |
| `make unlock` | Clears stale git lock files (see "Git on a FUSE-backed folder" below) |
| `make commit MSG='...'` | Unlocks, stages everything, commits |
| `make clean` | Removes the scratch `/tmp/podcast_run.json` file |

`chat`/`sms`/`email`/`html` each depend on `run`, so calling any one of them
alone re-queries the database fresh; `make all` only queries once and reuses
that same snapshot for all four renders (important, since each run also
advances the "since last check" stats window — you don't want `make all` to
silently zero that out by running the query four times in a row).

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
python3 render_report.py /tmp/run.json email  # SUBJECT: ... / body
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
  (subject + body), or a styled standalone HTML report.
- `reports/podcast_report.html` — the most recently generated HTML report.
- `Makefile` — convenience commands wrapping the two scripts above; see
  "Quick commands" below.
- `scripts/git_unlock.py` — clears stale git lock files on filesystems that
  block deletes; see "Git on a FUSE-backed folder" below.
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

## Git on a FUSE-backed folder

If this repo lives on a filesystem that allows renames but rejects deletes
(this happened with a Cowork-mounted folder during development — `unlink()`
fails with `EPERM` even though `rename()` works fine), plain git commands
can start failing with things like:

```
fatal: Unable to create '.../.git/index.lock': File exists.
```

That's git leaving lock/tmp files behind because it couldn't clean up after
itself. Run `make unlock` (or `python3 scripts/git_unlock.py` directly) to
clear them, or just use `make commit MSG='...'`, which unlocks first
automatically. This is a no-op and harmless to run on a normal filesystem —
skip this entirely if you're not hitting the error above.

## Known caveats

- The unplayed-queue detection (`ZUNPLAYEDTAB=1` for subscribed podcasts,
  one episode per podcast — its newest unplayed — capped to the 12 most
  recent across all shows, via `LATEST_EPISODES_LIMIT` in
  `podcast_summary.py`) was reverse-engineered from Apple's undocumented
  internal schema by comparing SQL query output against screenshots of the
  actual Podcasts.app "Latest Episodes" view. It is a heuristic, not
  documented Apple behavior — it could break on a different library, macOS
  version, or Podcasts app update. Two earlier versions of this heuristic
  were tried and disproven by screenshot: a 20-hour publish-gap cluster, and
  an uncapped one-per-podcast list (which surfaced years-old unplayed
  episodes from dormant subscriptions). The count of 12 was inferred, not
  confirmed as an official constant — if your queue ever looks off by a
  fixed number of episodes, that's the first thing to re-verify against a
  fresh screenshot.
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
