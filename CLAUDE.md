# CLAUDE.md

Guidance for Claude Code when working in this repo.

## What this is

A reference implementation, not a shipped product. It reads one person's
local Apple Podcasts SQLite library and generates a summary of the unplayed
"Latest Episodes" queue plus listening stats, with a letter grade for how far
behind the oldest unplayed episode is. Read `README.md` for the full picture
and `SKILL.md` for how an AI agent is meant to drive it. One-paragraph
version:

```
podcast_summary.py   queries the Podcasts DB, writes JSON to stdout
render_report.py     renders that JSON into chat / sms / email / html
Makefile             thin wrapper around the two scripts above — use this
scripts/git_unlock.py   clears stale git locks (see "FUSE folder" below)
```

Apple's Podcasts database schema (`MTLibrary.sqlite`) is undocumented and
private. Anything derived from it — especially the unplayed-queue detection
in `get_unplayed_queue()` — is a reverse-engineered heuristic validated
against one person's library at one point in time, not documented Apple
behavior. **Don't assume a heuristic here is correct just because it's in
the code** — verify against the actual Podcasts.app UI (screenshot from the
user) when in doubt, the same way past fixes in this repo were derived.

## Commands

`make` is the source of truth — prefer it over calling the Python directly:

```bash
make setup    # first run only: copies .env.example -> .env
make run      # queries the DB once, writes /tmp/podcast_run.json
make chat     # chat summary (depends on run — fresh query every time)
make sms      # SMS text
make email    # email SUBJECT + body
make html     # regenerates reports/podcast_report.html
make open     # regenerates html + opens it in the real macOS default browser
make discord  # posts the chat summary to Discord (needs DISCORD_WEBHOOK_URL)
make all      # runs the query ONCE, then renders chat+sms+email+html from
              # that one snapshot — always use this over calling chat/sms/
              # email/html separately if you want more than one format,
              # since each fresh query also advances the "since last check"
              # stats window
make unlock   # clear stale git locks — see "FUSE folder" below
make commit MSG='...'   # unlock, stage everything, commit
```

No external Python dependencies — standard library only, including a
minimal built-in `.env` loader. Nothing to `pip install`.

## Git workflow

- **Branch naming:** `YYYY.MM.DD/short-description`, e.g.
  `2026.08.16/fix-queue-and-now-playing` — date the branch was created, then
  a short kebab-case description. Don't use plain feature-name branches
  without the date prefix.
- **Never commit directly to `main`.** Branch off `main`, open a PR when
  ready.
- This repo's working copy sometimes lives on a FUSE-backed mount that
  allows renames but rejects deletes, which leaves stale
  `.git/index.lock`/`tmp_obj_*` files behind and makes plain `git` commands
  fail with `Unable to create '.../.git/index.lock': File exists.` Run
  `make unlock` (or `python3 scripts/git_unlock.py`) first if you hit that —
  it's a harmless no-op on a normal filesystem.
- The repo directory itself has a non-ASCII character before "Podcasts" in
  its name (visually looks like a plain space, isn't one) — don't assume a
  literal `" Podcasts"` path will `cd` correctly; resolve it programmatically
  (e.g. glob-match `*Podcasts` under `~/repos/`) rather than hardcoding it.

## Conventions

- **Env-driven, nothing hardcoded.** Delivery targets (`REPORT_EMAIL`,
  `REPORT_PHONE`, the Discord webhook, etc.) all come from `.env` / the
  JSON `config` block — never hardcode a personal email, phone number, or
  webhook URL into the Python or into a skill.
- **One DB query per report run.** `podcast_summary.py` advances
  `.podcast_skill_state.json`'s "since last check" timestamp every time it
  runs — don't call it more than once per logical report (`make all`, not
  four separate `make chat && make sms && ...`).
- **Delivery (email/text/Discord) is automatic, gated by `.env`.**
  `make email` / `make sms` / `make discord` each send for real via
  `scripts/send_email.py` (Outlook), `scripts/send_sms.py` (Messages.app),
  and `scripts/post_discord.py` (webhook) - and each skips cleanly if its
  destination variable is blank. There's no per-run confirmation step;
  consent is expressed by what's configured in `.env`. See `SKILL.md`.
- No test suite exists. Verify changes to `podcast_summary.py` /
  `render_report.py` by actually running `make all` against the real
  library and eyeballing the output — there's no library to fake it with, so
  a mocked test would just re-encode assumptions about the reverse-engineered
  schema instead of catching drift from it.
