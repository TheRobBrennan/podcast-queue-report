#!/usr/bin/env python3
"""
Send the report by email via Microsoft Outlook (AppleScript). Reads stdin
in the format `render_report.py ... email` produces:

    SUBJECT: <subject line>
    ---
    <body>

`body` is the same styled HTML as `render_report.py ... html` - the email
is the web report, not a separate plain-text summary.

Reads REPORT_EMAIL from .env (same minimal loader pattern as
podcast_summary.py). Leave REPORT_EMAIL blank/unset to skip sending —
this exits 0 so it doesn't break a `make all` chain.

Usage:
    python3 render_report.py /tmp/podcast_run.json email | python3 scripts/send_email.py
    make email
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
    to_address = os.environ.get("REPORT_EMAIL")
    if not to_address:
        print("REPORT_EMAIL not set in .env — skipping email.", file=sys.stderr)
        sys.exit(0)

    raw = sys.stdin.read()
    if not raw.strip():
        print("Nothing to send (empty input).", file=sys.stderr)
        sys.exit(1)

    first_line, _, rest = raw.partition("\n")
    subject = first_line.removeprefix("SUBJECT:").strip()
    body_html = rest.split("---\n", 1)[-1] if "---\n" in rest else rest

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as subject_f:
        subject_f.write(subject)
        subject_path = subject_f.name
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False) as body_f:
        body_f.write(body_html)
        body_path = body_f.name

    script = f'''
set subjectText to (read POSIX file "{subject_path}" as «class utf8»)
set bodyHtml to (read POSIX file "{body_path}" as «class utf8»)
tell application "Microsoft Outlook"
    activate
    set newMessage to make new outgoing message with properties {{subject:subjectText, content:bodyHtml}}
    tell newMessage
        make new to recipient at newMessage with properties {{email address:{{address:"{to_address}"}}}}
    end tell
    send newMessage
end tell
'''

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Outlook send failed: {result.stderr.strip()}", file=sys.stderr)
            sys.exit(1)
        print(f"Emailed report to {to_address}.")
    finally:
        os.unlink(subject_path)
        os.unlink(body_path)


if __name__ == "__main__":
    main()
