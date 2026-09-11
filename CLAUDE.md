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
make cron     # discord + email from one query (used by the launchd agent)
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
- **Include a screenshot in the PR for any visible-output change** (chat/
  terminal, HTML, email, SMS, Discord). Generate it headlessly - don't rely
  on the interactive Simulator/screen-recording permission:
  - HTML/email: `make html`, then screenshot the file with headless Chrome,
    e.g. `google-chrome --headless --disable-gpu --screenshot=out.png
    --window-size=900,2400 file:///path/to/reports/podcast_report.html`.
  - Chat/terminal/SMS: capture the text output (`make chat` etc.), wrap it
    in a small dark-background/monospace HTML page, then screenshot that
    the same way - there's no real terminal window to capture headlessly.
  Save the PNG under `assets/`, commit it on the PR branch, and embed it in
  the PR body via the raw GitHub URL (`https://raw.githubusercontent.com/
  TheRobBrennan/podcast-queue-report/<branch>/assets/<file>.png`) - a
  relative path or `../blob/...` link does not resolve in a PR body.
- This repo's working copy sometimes lives on a FUSE-backed mount that
  allows renames but rejects deletes, which leaves stale
  `.git/index.lock`/`tmp_obj_*` files behind and makes plain `git` commands
  fail with `Unable to create '.../.git/index.lock': File exists.` Run
  `make unlock` (or `python3 scripts/git_unlock.py`) first if you hit that —
  it's a harmless no-op on a normal filesystem.
- The repo directory name begins with **U+F8FF (the Apple logo glyph, )**
  followed by a space — ` Podcasts`. It looks like a plain leading space
  and isn't one. Don't assume a literal `" Podcasts"` path will `cd`
  correctly; resolve it programmatically (glob-match `*Podcasts` under
  `~/repos/`, or build it with `printf '\uf8ff'`) rather than hardcoding it.
  Confirm the bytes with
  `ls ~/repos/ | grep -i podcast | xxd | head -3` — expect `ef a3 bf`.
  This bites hardest outside the shell, where a glob isn't available: a
  launchd plist `ProgramArguments` path typed with a plain space fails to
  spawn and reports `exit code 78: EX_CONFIG` with an **empty log file**,
  which reads as a config or permissions problem rather than a bad path.

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
- **`make test` covers the deterministic logic, not the reverse-engineered
  heuristics.** `tests/test_podcast_summary.py` builds a synthetic
  in-memory SQLite schema and checks that `get_unplayed_queue()` /
  `get_duplicate_episodes()` / `grade_for_days()` correctly implement
  their own stated rules — window bounding, entitlement/cross-promo/
  played exclusions, per-podcast dedup. It catches logic regressions
  (e.g. a started episode silently exempted from the window, which is
  what shipped as a real bug on 2026-09-10) that pure eyeballing can
  miss. It does NOT and cannot validate that ZPLAYSTATE, ZUNPLAYEDTAB,
  ZENTITLEMENTSTATE etc. mean what we think they mean in the real
  library — that's a claim about Apple's undocumented schema, and the
  only way to check it is against the real Podcasts.app UI (screenshot
  from the user). So: run `make test` for logic changes, but still
  verify against `make all` and a fresh screenshot whenever a change
  touches what counts as "unplayed" in the first place.
