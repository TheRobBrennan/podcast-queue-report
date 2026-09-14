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
# Safe to re-run any time - a `brew upgrade python@3.14` overwrites the
# binary and wipes this signature, so re-run this script after any such
# upgrade (`codesign -dv <python3.14 path>` shows `Signature=adhoc` again
# when that's happened).
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

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT

if security find-certificate -c "$CERT_NAME" "$KEYCHAIN" &>/dev/null; then
  echo "Certificate '$CERT_NAME' already exists in $KEYCHAIN - reusing it."
else
  echo "Creating self-signed code-signing certificate '$CERT_NAME'..."

  openssl req -x509 -newkey rsa:2048 -keyout "$TMPDIR/key.pem" -out "$TMPDIR/cert.pem" \
    -days 3650 -nodes -subj "/CN=$CERT_NAME" \
    -addext "extendedKeyUsage=codeSigning" \
    -addext "basicConstraints=critical,CA:true" \
    2>/dev/null

  # Random one-time password for the intermediate .p12 - it's discarded
  # the moment `security import` finishes with it, never stored.
  #
  # -legacy: OpenSSL 3.x defaults to AES-256/SHA-256 for PKCS12, which
  # macOS's `security import` (built on the older Apple CDSA PKCS12
  # parser) cannot read - it fails with "MAC verification failed during
  # PKCS12 import (wrong password?)" even with the correct password.
  # -legacy switches back to the RC2/3DES+SHA-1 encoding Keychain
  # actually understands.
  P12_PASS="$(openssl rand -base64 24)"
  openssl pkcs12 -export -legacy -out "$TMPDIR/cert.p12" \
    -inkey "$TMPDIR/key.pem" -in "$TMPDIR/cert.pem" \
    -passout "pass:$P12_PASS" 2>/dev/null

  # -T /usr/bin/codesign pre-authorizes codesign to use this key without
  # a "keychain wants to use a key" prompt on every future sign.
  security import "$TMPDIR/cert.p12" -k "$KEYCHAIN" -P "$P12_PASS" \
    -T /usr/bin/codesign -T /usr/bin/security

  echo "Certificate created and imported into $KEYCHAIN."
fi

# A freshly-imported self-signed cert sits in the keychain but isn't yet
# *trusted* for code signing - codesign only picks identities that pass
# code-signing trust evaluation (`security find-identity -p codesigning`),
# so `codesign -s "$CERT_NAME"` fails with "no identity found" until this
# runs. This is a keychain trust setting, not a system trust root - it
# does not require sudo or an admin password, though macOS may show a
# one-time confirmation dialog; click Always Allow/Trust if so.
if ! security find-identity -v -p codesigning "$KEYCHAIN" 2>/dev/null | grep -q "$CERT_NAME"; then
  echo "Trusting '$CERT_NAME' for code signing..."
  security find-certificate -c "$CERT_NAME" -p "$KEYCHAIN" > "$TMPDIR/existing_cert.pem"
  security add-trusted-cert -p codeSign -k "$KEYCHAIN" "$TMPDIR/existing_cert.pem"
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
