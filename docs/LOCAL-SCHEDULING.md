# Local scheduling (launchd)

The report runs itself on this MacBook Pro via a **launchd user agent**, with no
Claude session in the loop. Everything the report needs is a plain shell
pipeline (`make discord`), so a scheduled agent is all it takes.

This replaces the Claude cloud routine that used to drive it. That routine is
disabled as of 2026-09-04.

## Why launchd and not cron

The problem with the cloud routine was missed fires piling up: if the machine
was asleep or unavailable across several scheduled times, the runs would land
back-to-back and spam the channel.

`launchd` solves this directly. With `StartCalendarInterval`, any intervals
missed while the machine is asleep **coalesce into a single run on wake** - not
one run per missed slot. `cron` does the opposite (it simply skips missed runs
entirely and has no wake behavior), which is why it isn't used here.

There is a second belt in the wrapper script: `MIN_GAP_MINUTES` (default 60)
makes the script exit early if a *successful* run finished less than that many
minutes ago. The real schedule's tightest gap is 2 hours, so this only ever
fires if something unexpected triggers a double run.

The agent also sets `RunAtLoad`, so it fires once whenever it (re)loads - at
login, reboot, or a manual `bootout`/`bootstrap`. This only covers the moment
the agent loads, not periodic runs while asleep - `StartCalendarInterval`
fires still require the machine to already be awake with an active session
(see "Why launchd and not cron" above); a `LaunchAgent` cannot wake the
machine itself. `RunAtLoad` just closes the common case of "asleep overnight,
opened the lid this morning" a bit sooner than waiting for the next
`StartCalendarInterval` fire to coalesce.

## Schedule

| Time     | Gap since previous |
| -------- | ------------------ |
| 7:06am   | 8h                 |
| 9:06am   | 2h                 |
| 11:06am  | 2h                 |
| 1:06pm   | 2h                 |
| 3:06pm   | 2h                 |
| 5:06pm   | 2h                 |
| 7:06pm   | 2h                 |
| 11:06pm  | 4h                 |
| 3:06am   | 4h                 |

Every 2 hours across the 7am-7pm working-hours window, then 4-hour gaps
overnight. Tightening the daytime gap only helps while the Mac is actually
awake (lid open, even with the display dimmed) - it buys nothing while
genuinely asleep, since missed fires coalesce into one run either way
regardless of how many were scheduled in between.

## Files

- `deploy/ai.sploosh.podcast-queue-report.plist` - the agent definition. The
  installed copy lives at
  `~/Library/LaunchAgents/ai.sploosh.podcast-queue-report.plist`; this one is
  the version-controlled source of truth.
- `scripts/launchd_run.sh` - what the agent actually executes. Sets a
  launchd-safe `PATH`, checks `.env` exists, applies the `MIN_GAP_MINUTES`
  guard, runs `make discord`, and writes a timestamped line either way.

## Install / update

```bash
cp deploy/ai.sploosh.podcast-queue-report.plist ~/Library/LaunchAgents/
plutil -lint ~/Library/LaunchAgents/ai.sploosh.podcast-queue-report.plist
launchctl bootout gui/$UID/ai.sploosh.podcast-queue-report 2>/dev/null
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/ai.sploosh.podcast-queue-report.plist
```

`bootout` before `bootstrap` on every edit - launchd does not pick up plist
changes on its own.

## Verify

```bash
# is it loaded, and did the last run succeed?
launchctl print gui/$UID/ai.sploosh.podcast-queue-report | grep -E "state|runs =|last exit"

# what happened, and when
tail -20 ~/Library/Logs/podcast-queue-report/run.log
```

**Check `last exit code` first, not the log.** A job that fails to spawn never
runs the script, so it appends nothing - a stale log looks identical to a
healthy idle one. `last exit code = 78: EX_CONFIG` means launchd could not
execute the program, which in practice means the path is wrong.

Force a run without waiting for the schedule:

```bash
launchctl kickstart -k gui/$UID/ai.sploosh.podcast-queue-report
```

To test the pipeline **without posting to Discord**, run the wrapper with
`DRY_RUN=1` - it queries the Podcasts database and stops before the post:

```bash
DRY_RUN=1 ./scripts/launchd_run.sh
```

Note that even a dry run advances the "since last check" stats window, because
it still executes `podcast_summary.py`.

## Disable

```bash
launchctl bootout gui/$UID/ai.sploosh.podcast-queue-report
rm ~/Library/LaunchAgents/ai.sploosh.podcast-queue-report.plist
```

## Gotchas

### The repo path starts with the Apple logo glyph, not a space

The repo directory is `/Users/rob/repos/ Podcasts` - that leading character is
**U+F8FF (the Apple logo,  ), followed by a space**. It is not a plain leading
space, and several notes previously described it as one.

This matters because a plist `ProgramArguments` path typed with a plain space
fails to spawn, and launchd reports it as `exit code 78: EX_CONFIG` with an
**empty log file** - which looks like a config or permissions problem rather
than a bad path. If you hit that, check the bytes:

```bash
ls /Users/rob/repos/ | grep -i podcast | xxd | head -3   # expect ef a3 bf
```

Do not type the glyph into the plist at all - **generate the plist
programmatically**, resolving the path from the filesystem:

```python
import plistlib, glob, os
script = os.path.join(glob.glob("/Users/rob/repos/*Podcasts")[0], "scripts", "launchd_run.sh")
assert os.path.exists(script)
# ...build the dict, plistlib.dump it, then read it back and assert again
```

**This regressed once, on 2026-09-04.** The bug was found, fixed, and written up
here - and then the plist was rewritten by hand one last time to change the
schedule, which silently put a plain space back in. The dry-run verification had
happened *before* that final rewrite, so the agent was reported as working while
the installed plist pointed at a file that does not exist. Every scheduled run
failed for two days and posted nothing. Nobody noticed until 2026-09-06, because
a failure to spawn writes **nothing to the log** - the last line was still the
successful dry run, which reads exactly like a healthy idle agent.

The glyph is also fragile in transit: it can be silently normalised to a plain
space when pasted between tools, editors, or chat. That is the same reason the
regression happened. Never retype it; always resolve it from the filesystem.

Two rules follow:

1. After **any** plist edit, read the path back out and assert it exists before
   bootstrapping. `plutil -lint` does not catch this - the file was valid XML
   the whole time.
2. A `kickstart` is only evidence for the plist installed *right now*. Verify
   after the last edit, not before it.

### Full Disk Access

Reading the Apple Podcasts database from a launchd agent requires Full Disk
Access for the executing binary. This is already working on this machine
(verified 2026-09-04 via a `DRY_RUN=1` kickstart). If it ever breaks after an
OS upgrade, the symptom is a permissions error from `podcast_summary.py` in
`run.log`; grant Full Disk Access to `/bin/zsh` in System Settings ->
Privacy & Security.

### Discord only

The agent runs `make discord`, not `make all`. `make all` drags in the `email`
and `sms` targets, which are deliberately disabled (`REPORT_PHONE` and
`REPORT_EMAIL_CLIENT` commented out in `.env`) because they need computer-use
control of Messages/Outlook and can't run unattended.
