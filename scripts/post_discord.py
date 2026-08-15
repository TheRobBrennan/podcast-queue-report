#!/usr/bin/env python3
"""
Post a report to a Discord channel via webhook. Reads a webhook payload
from stdin — either a JSON object (used as-is, e.g. an {"embeds": [...]}
payload from `render_report.py ... discord`) or plain text (wrapped as
{"content": ...}, truncated to Discord's 2000-char message cap).

Reads DISCORD_WEBHOOK_URL from .env (same minimal loader pattern as
podcast_summary.py — never hardcode the URL here).

Usage:
    python3 render_report.py /tmp/podcast_run.json discord | python3 scripts/post_discord.py
    make discord
"""
import json
import os
import sys
import urllib.request

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DISCORD_LIMIT = 2000


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
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set in .env — skipping Discord post.", file=sys.stderr)
        sys.exit(1)

    raw = sys.stdin.read().strip()
    if not raw:
        print("Nothing to post (empty input).", file=sys.stderr)
        sys.exit(1)

    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        content = raw
        if len(content) > DISCORD_LIMIT:
            content = content[: DISCORD_LIMIT - 20].rstrip() + "\n… (truncated)"
        payload = {"content": content}

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "podcast-queue-report/1.0 (+https://github.com/TheRobBrennan/podcast-queue-report)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"Posted to Discord (status {resp.status}).")
    except urllib.error.HTTPError as e:
        print(f"Discord post failed: {e.code} {e.read().decode(errors='replace')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
