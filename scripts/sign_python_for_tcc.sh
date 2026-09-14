#!/usr/bin/env bash
# Gives the Homebrew python3.14 binary a stable, non-content-hash code
# signature so macOS's "access data from other apps" permission (TCC
# service kTCCServiceSystemPolicyAppData - the dialog guarding
# podcast_summary.py's direct read of Podcasts.app's Group Container
# database) actually persists across separate process launches.
#
# Root cause (see docs/LOCAL-SCHEDULING.md): Homebrew ships python3.14
# ad-hoc signed (`codesign -dv` shows `Signature=adhoc`, no Team ID). TCC
# ties this particular permission's persistence to code identity, and an
# ad-hoc signature - keyed off a hash of the binary's own bytes - isn't
# durable enough: it was observed re-prompting seconds after being
# granted, for the exact same unchanged file. Every other app on this
# Mac holding a stable grant for this permission (Claude Code, VS Code,
# Terminal, Codex) is properly Developer-ID signed. Re-signing with any
# real certificate - even a self-signed one - switches TCC's identity key
# from "hash of these bytes" to "signed by this certificate," which is
# what makes the grant stick.
#
# This script does NOT create the certificate - a hand-rolled openssl
# cert reliably fails codesign's own trust check in subtle ways (missing
# key-usage bits, a leaf marked CA:true, etc.), even after
# `security add-trusted-cert` reports success. Apple's own Certificate
# Assistant wizard is built for exactly this and doesn't have that
# problem, so create the identity there ONCE:
#
#   1. Open Keychain Access.
#   2. Menu: Keychain Access -> Certificate Assistant -> Create a Certificate...
#   3. Name it (this script's default expects "podcast-report-python-codesign",
#      or set CODESIGN_IDENTITY to whatever you named it).
#   4. Identity Type: Self Signed Root. Certificate Type: Code Signing.
#   5. Click Create, then Done. Keychain Access sets up trust correctly
#      as part of this flow - if it doesn't ask, or codesign still can't
#      find it, open the new cert in Keychain Access, expand "Trust", and
#      set "Code Signing" to "Always Trust" (enter your password if asked
#      - that's macOS's own trust-setting dialog, not this script).
#
# This script just does the repeatable part: re-signing the binary with
# whatever identity you created above. Safe to re-run any time - a
# `brew upgrade python@3.14` overwrites the binary and wipes its
# signature, so re-run this after any such upgrade (`codesign -dv
# <python3.14 path>` shows `Signature=adhoc` again when that's happened).
set -euo pipefail

CERT_NAME="${CODESIGN_IDENTITY:-podcast-report-python-codesign}"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

PY_BIN="$(python3.14 -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
if [[ -z "$PY_BIN" ]]; then
  echo "FAIL: python3.14 not found on PATH" >&2
  exit 1
fi
PY_BIN="$(readlink -f "$PY_BIN" 2>/dev/null || greadlink -f "$PY_BIN" 2>/dev/null || echo "$PY_BIN")"

echo "Target binary: $PY_BIN"

if ! security find-identity -v -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$CERT_NAME"; then
  cat >&2 <<EOF

FAIL: no valid code-signing identity named '$CERT_NAME' found in $KEYCHAIN.

Create it once via Keychain Access (see the comment block at the top of
this script for the exact steps), then re-run this script. If you named
it something other than '$CERT_NAME', set CODESIGN_IDENTITY:

  CODESIGN_IDENTITY="Your Cert Name" bash scripts/sign_python_for_tcc.sh
EOF
  exit 1
fi

echo "Re-signing $PY_BIN ..."
codesign --force --sign "$CERT_NAME" "$PY_BIN"

echo ""
echo "Before:"
echo "  Signature=adhoc, TeamIdentifier=not set"
echo "After:"
codesign -dv "$PY_BIN" 2>&1 | grep -E "Signature|TeamIdentifier|Identifier"

echo ""
echo "Done. Run 'make run' once now and click Allow if the \"access data"
echo "from other apps\" dialog appears - that grant should now stick,"
echo "since it's keyed to the certificate instead of the binary's bytes."
