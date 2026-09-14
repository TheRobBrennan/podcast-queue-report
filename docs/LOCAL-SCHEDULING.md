# Local scheduling (launchd)

The report runs itself on this MacBook Pro via a **launchd user agent**, with no
Claude session in the loop. Everything the report needs is a plain shell
pipeline (`make cron`), so a scheduled agent is all it takes.

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
  guard, runs `make cron`, and writes a timestamped line either way.

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

**python3.14 needs Full Disk Access, added manually in System Settings ->
Privacy & Security -> Full Disk Access.** This section previously claimed
this was "already working" via `/bin/zsh` - that was wrong; see the next
section for how that was discovered and fixed for real on 2026-09-13. If it
ever breaks after an OS upgrade or a Python reinstall, the symptom is either
a permissions error from `podcast_summary.py` in `run.log`, or (more often
for this specific path) a run that just hangs at "Catching up on your
queue..." for minutes with nothing further logged - re-check the toggle is
still on for `python3.14`'s exact path under Full Disk Access.

### Email + Discord + open-in-browser, not SMS

The agent runs `make cron`: email (Outlook) + Discord post + opening the
HTML report in the default browser. This went through two changes worth
knowing about if you're reading history: the 2026.09.12 fix (`d07e16b` /
[#27](https://github.com/TheRobBrennan/podcast-queue-report/pull/27))
removed email from `make cron` in favor of just opening the browser, then a
later change brought email back alongside Discord and the browser open - so
`make cron` now sends all three every scheduled run. `make all` (which also
drags in the `sms` target) is still never used for the scheduled agent;
Messages.app automation is noisier to run unattended, so `REPORT_PHONE`
stays blank in `.env` for this path regardless.

### One-time Automation grant for System Events

`podcast_summary.py`'s `refresh_podcasts_feeds()` (added in `f92a441`, to fix
an unplayed-queue undercount - see `CLAUDE.md`) shells out to `osascript` on
**every** report run to ask System Events whether Podcasts.app is running,
and if so to click its "Refresh Feeds" menu item. That is a distinct Apple
Events target from anything else in this repo, so the first time it ever
runs, macOS raises an Automation permission dialog asking to control System
Events, separate from (and unrelated to) any permission ever granted to
`osascript` for Outlook/Mail.

This is a one-time, per-binary OS grant, not a per-run one. Click Allow and
it persists in System Settings -> Privacy & Security -> Automation, listed
under `python3.14`; every run after that returns in well under a second with
no dialog. It only needs re-granting if the underlying `python3.14` binary
is ever replaced (e.g. a Homebrew version bump), since that changes the code
identity macOS ties the grant to.

The one hazard is racing the dialog itself: the check originally used a 5s
`subprocess` timeout, short enough that a slow click could get the process
killed before an answer was recorded - the decision is then never persisted
and every subsequent run re-prompts. That call now uses a 30s timeout (see
the comment at `podcast_summary.py:357`) precisely so a first-time click has
time to land; it costs nothing on every run after the first since a granted
permission responds almost immediately.

**This section previously (incorrectly) attributed the dialog text
`"python3.14" would like to access data from other apps` to this Automation
grant. It doesn't belong here - see the next section, which is the dialog
that text actually belongs to and the one that kept recurring after this
fix shipped.**

### The recurring "access data from other apps" dialog (2026-09-13)

The Sep 12 fix above did **not** stop the periodic re-prompting Rob kept
hitting - because it was fixing a different dialog than the one actually
recurring. The one that kept coming back reads exactly:

> "python3.14" would like to access data from other apps.

with a folder-and-hand icon - that's macOS's `kTCCServiceSystemPolicyAppData`
protection ("App Data" in some tooling), not Automation. It's triggered by
`podcast_summary.py` connecting directly to Podcasts.app's private database
at `~/Library/Group Containers/243LU875E5.groups.com.apple.podcasts/Documents/MTLibrary.sqlite`
(see `_resolve_db_path()`) - a raw file read into another app's container,
which is exactly what this TCC service guards.

**First theory (wrong): ad-hoc code signature.** Homebrew ships `python3.14`
ad-hoc signed (`codesign -dv` showed `Signature=adhoc`, `TeamIdentifier=not
set`), and every other app on the Mac with a *stable* grant for this TCC
service (Claude Code, VS Code, Terminal, Codex) is properly Developer-ID
signed - a reasonable-looking correlation that turned out not to be
causal. Re-signing `python3.14` with a real self-signed certificate (via
Keychain Access's Certificate Assistant - a hand-rolled `openssl req` cert
failed `codesign`'s own trust check twice in a row, in ways that looked
successful right up until "no identity found") did **not** fix it: two
more back-to-back `launchctl kickstart -k` runs after re-signing, each a
separate process launch, each needed its own fresh Allow click.

**Actual root cause:** per a [reported GitHub issue](https://github.com/stablyai/orca/issues/8381)
describing the identical symptom for an unrelated tool, `kTCCServiceSystemPolicyAppData`
grants are stored keyed to `session_pid` and `session_boot_UUID` - the
*process ID of whichever run asked* - not to a stable code identity at all.
Every new process launch is, by definition, a new PID, so **this permission
is architecturally incapable of persisting across separate short-lived CLI
invocations, signed or not.** The long-running GUI apps that appeared to
hold "stable" grants (Claude Code, VS Code, Terminal) aren't actually proof
that signing matters here - they just don't relaunch as a fresh process on
every operation the way a scheduled CLI script does, so their prompt only
ever happens once per app *launch*, which for a GUI app is rare.

**Real fix: Full Disk Access**, a separate, stable, path-based grant
(`kTCCServiceSystemPolicyAllFiles`) that supersedes this session-scoped
check entirely - confirmed empty for `python3.14` before this fix. Added
manually (this is a System Settings toggle, not something scriptable):

1. System Settings -> Privacy & Security -> **Full Disk Access**.
2. Click **+**, `Cmd+Shift+G`, paste the real path:
   `/opt/homebrew/Cellar/python@3.14/3.14.6/Frameworks/Python.framework/Versions/3.14/bin/python3.14`
3. Add it, confirm the toggle is on.

Verified with three back-to-back `launchctl kickstart -k` runs immediately
after enabling it - each completed in ~24s with no dialog, versus every
prior test (ad-hoc *and* signed) hanging for minutes waiting on a click.

The code-signing work above (`scripts/sign_python_for_tcc.sh`, the
Keychain Access certificate) is harmless but was **not the fix** - it's
left in place since a real signature doesn't hurt anything and the
Automation grant (previous section) genuinely does benefit from code
identity stability, just not this one.

To prime the grant manually instead of waiting for a scheduled run to
trigger it, run the same check launchd would run, from Terminal, while
you're at the keyboard to click Allow:

```bash
/opt/homebrew/bin/python3.14 -c '
import subprocess
r = subprocess.run(
    ["osascript", "-e", "tell application \"System Events\" to exists application process \"Podcasts\""],
    capture_output=True, text=True, timeout=30,
)
print(r.returncode, r.stdout.strip(), r.stderr.strip())
'
```

`0 true` with no dialog means it is already granted.
