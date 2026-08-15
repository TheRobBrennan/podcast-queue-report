#!/usr/bin/env python3
"""
Clear stale git lock/tmp files.

Some filesystems (notably the FUSE-backed folder this repo has been run
from) allow `rename()` but reject `unlink()` (delete) with EPERM. Git relies
on delete-then-recreate in a few places (index.lock, HEAD.lock, loose-object
tmp files, pack tmp files) — on a filesystem like that, git leaves these
files behind after almost every command, and the *next* git command then
fails with something like:

    fatal: Unable to create '.../.git/index.lock': File exists.

This script finds those leftover files and renames them out of the way
(".stale" suffix) instead of trying to delete them, which is enough to
unblock git. Safe to run any time, including when nothing is stuck — it
just does nothing in that case.

Usage:
    python3 scripts/git_unlock.py
    make unlock
"""
import glob
import os
import sys

PATTERNS = [
    ".git/*.lock",
    ".git/**/*.lock",
    ".git/objects/*/tmp_obj_*",
    ".git/objects/pack/tmp_pack_*",
]


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(repo_root)

    cleared = []
    for pattern in PATTERNS:
        for path in glob.glob(pattern, recursive=True):
            if path.endswith(".stale"):
                continue
            try:
                os.rename(path, path + ".stale")
                cleared.append(path)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f"could not clear {path}: {e}", file=sys.stderr)

    if cleared:
        print(f"Cleared {len(cleared)} stale git lock/tmp file(s):")
        for path in cleared:
            print(f"  {path}")
    else:
        print("No stale git lock files found.")


if __name__ == "__main__":
    main()
