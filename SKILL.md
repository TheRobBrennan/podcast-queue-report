# Podcast Queue Report skill

Reference instructions for an AI agent (e.g. Claude in Claude Code or Cowork)
to generate a report on the user's Apple Podcasts unplayed queue. Delivery
(email, SMS, Discord) is automated via `make all` / `npm start`, gated
entirely by which of REPORT_EMAIL, REPORT_PHONE, and DISCORD_WEBHOOK_URL
are set in `.env` - leaving one blank skips that channel. There is no
per-run confirmation step; consent is expressed by configuring `.env`.

> This skill assumes the repo has already been cloned and configured per the
> "Setup" section of `README.md` (i.e. `.env` exists next to these scripts
> with real values filled in). It is a reference implementation validated on
> one specific Mac only — see `README.md`'s caveats before relying on it.

## What this skill does

1. Runs `podcast_summary.py` in this repo to query the local Apple Podcasts
   library and produce JSON describing the current unplayed "Latest
   Episodes" queue (count, total time, oldest episode, a letter grade for
   how many days behind the user is) plus played-episode stats for several
   time windows (since last check, yesterday, past week, this month, all
   time).
2. Runs `render_report.py` against that JSON to produce a chat summary, an
   SMS-length text, an email subject/body, a full HTML report, and/or a
   Discord embed.
3. Automatically delivers the report - `make email` (Outlook),
   `make sms` (Messages.app), and `make discord` (webhook) each skip
   cleanly if their `.env` destination isn't configured.

## Steps

1. `cd` into the repo (wherever it was cloned). Confirm `.env` exists; if
   not, stop and tell the user to complete Setup in `README.md` first.
2. Run `make all` (queries the Podcasts DB once, then renders chat + sms +
   email + html and sends email/sms/Discord from that single snapshot —
   see `README.md`'s "Quick commands" section). If you only need one format
   without sending anything, `make chat` / `make sms` / `make email` /
   `make html` also work individually, each doing its own fresh query.
   Don't call `podcast_summary.py` directly more than once per report — it
   advances the "since last check" stats window on every run. If this fails
   because the Podcasts database can't be found or opened, report the exact
   error to the user rather than guessing — do not fall back to fabricated
   data.
3. Show the chat summary in the conversation, and open the HTML report for
   the user to see:
   - **Running in a real shell on the Mac** (e.g. Claude Code with direct
     shell access, or the user's own Terminal): run `make open` — it uses
     the standard macOS `open` command, which launches the user's actual
     default browser. This is the preferred path whenever it's available.
   - **Running in Claude Cowork's sandboxed bash**: that shell is an
     isolated Linux container with no path to the user's real screen, so
     `open`/`make open` will not do anything visible. Do not try to work
     around this by taking over the user's screen via computer-use unless
     they're clearly not actively using their computer — check first, and
     back off immediately if you see signs of active, unrelated use (other
     open apps, personal content on screen, etc.). Instead, present the
     generated `reports/podcast_report.html` file to the user directly
     (e.g. via a file-sharing tool) so they can open it themselves with one
     click.
4. Delivery already happened as part of step 2 if `make all` was used - no
   separate send step, and no per-run confirmation prompt, since `.env`
   configuration is the consent mechanism (same model as Discord's
   `DISCORD_WEBHOOK_URL`). `make email` uses Outlook and `make sms`
   uses Messages.app (both AppleScript, macOS only) to send to
   `REPORT_EMAIL`/`REPORT_PHONE`; either skips with an explanatory message
   if its variable is blank. If the user asks to *stop* a channel from
   sending, comment out or blank that variable in `.env` rather than trying
   to intercept the send at runtime.
5. State updates automatically: `podcast_summary.py` records
   `.podcast_skill_state.json` (gitignored) with this run's timestamp, so
   the next run's "since last check" window starts from here.

## Notes for the agent

- Never invent or guess the destination email/phone — always read them
  from `.env` (the scripts do this automatically). If any are blank, that
  channel is skipped by design; tell the user it isn't configured rather
  than trying to work around it.
- If this environment sandboxes file access (e.g. Claude Cowork's bash
  sandbox vs. its Read/Write/Edit tools), be aware paths may differ between
  tools — resolve the real repo/database paths dynamically rather than
  hardcoding one path style.
- Never delete files with `rm -rf` or similar inside a synced/cloud folder
  without checking first that deletes actually work there — some FUSE-backed
  folders reject unlink operations even though renames succeed. If a git
  command in this repo fails with something like "Unable to create
  '.git/index.lock': File exists," run `make unlock` and retry — see
  `scripts/git_unlock.py`'s docstring for why this happens.
- To commit changes in this repo, prefer `make commit MSG='...'` over raw
  `git commit` — it clears stale lock files first automatically.
