#!/usr/bin/env python3
"""
Send the report as a text message via Messages.app (AppleScript). Reads
the SMS text from stdin (as produced by `render_report.py ... sms`) and
sends it to REPORT_PHONE over iMessage - falls back to SMS automatically
if that number isn't on iMessage and this Mac has Text Message
Forwarding enabled from a paired iPhone.

Reads REPORT_PHONE from .env (same minimal loader pattern as
podcast_summary.py). Leave REPORT_PHONE blank/unset to skip sending -
this exits 0 so it doesn't break a `make all` chain.

Usage:
    python3 render_report.py /tmp/podcast_run.json sms | python3 scripts/send_sms.py
    make send-sms
"""
import os
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    env_path = os.path.join(REPO_ROOT, ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main():
    _load_dotenv()
    phone = os.environ.get("REPORT_PHONE")
    if not phone:
        print("REPORT_PHONE not set in .env — skipping SMS.", file=sys.stderr)
        sys.exit(0)

    body = sys.stdin.read()
    if not body.strip():
        print("Nothing to send (empty input).", file=sys.stderr)
        sys.exit(1)

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as body_f:
        body_f.write(body)
        body_path = body_f.name

    script = f'''
set bodyText to (read POSIX file "{body_path}" as «class utf8»)
tell application "Messages"
    activate
    delay 1
    set targetService to 1st service whose service type = iMessage
    set targetBuddy to buddy "{phone}" of targetService
    send bodyText to targetBuddy
end tell
'''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"SMS send failed: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        print(f"Texted report to {phone}.")
    finally:
        os.unlink(body_path)


if __name__ == "__main__":
    main()
