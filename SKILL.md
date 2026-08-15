# Podcast Queue Report skill

Reference instructions for an AI agent (e.g. Claude in Claude Code or Cowork)
to generate — and, only with explicit user confirmation, deliver — a report
on the user's Apple Podcasts unplayed queue.

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
   SMS-length text, an email subject/body, and/or a full HTML report.
3. Optionally, with the user's explicit go-ahead for *that specific run*,
   delivers the report by text message and/or email.

## Steps

1. `cd` into the repo (wherever it was cloned). Confirm `.env` exists; if
   not, stop and tell the user to complete Setup in `README.md` first.
2. Run:
   ```bash
   python3 podcast_summary.py > /tmp/podcast_run.json
   ```
   If this fails because the Podcasts database can't be found or opened,
   report the exact error to the user rather than guessing — do not fall
   back to fabricated data.
3. Render what's needed:
   ```bash
   python3 render_report.py /tmp/podcast_run.json chat
   python3 render_report.py /tmp/podcast_run.json sms
   python3 render_report.py /tmp/podcast_run.json email
   python3 render_report.py /tmp/podcast_run.json html > reports/podcast_report.html
   ```
4. Show the chat summary in the conversation.
5. **Only if the user explicitly asks to send/text/email this run** (a
   standing schedule does not count as consent for an individual send —
   always confirm the specific run):
   - **Text message**: open Messages (native app, via computer use) and
     send the `sms` output to the phone number configured as `REPORT_PHONE`
     in `.env` (read it from the JSON's `config.phone` field — never
     hardcode a number in this skill).
   - **Email**: open the email client named in `.env`'s
     `REPORT_EMAIL_CLIENT` (read from `config.email_client`) — the user has
     specifically asked for their configured client, not necessarily the OS
     default Mail app. Compose to `config.email`, using the `SUBJECT:` line
     and body from the `email` render, and **attach**
     `reports/podcast_report.html` (browse to it directly via Finder/the
     attach dialog's folder navigation — a freshly-written file may not show
     up in a filename search yet due to indexing lag).
   - Always show the user what will be sent and to whom before hitting
     send, and wait for confirmation.
6. State updates automatically: `podcast_summary.py` records
   `.podcast_skill_state.json` (gitignored) with this run's timestamp, so
   the next run's "since last check" window starts from here.

## Notes for the agent

- Never invent or guess the destination email/phone/client — always read
  them from `.env` via the JSON `config` block. If any are blank, skip that
  delivery channel and tell the user it isn't configured.
- If this environment sandboxes file access (e.g. Claude Cowork's bash
  sandbox vs. its Read/Write/Edit tools), be aware paths may differ between
  tools — resolve the real repo/database paths dynamically rather than
  hardcoding one path style.
- Never delete files with `rm -rf` or similar inside a synced/cloud folder
  without checking first that deletes actually work there — some FUSE-backed
  folders reject unlink operations even though renames succeed.
