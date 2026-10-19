#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check pull-request commits for a matching DCO Signed-off-by trailer."""
from __future__ import annotations
import os
import subprocess


def main() -> int:
    base_ref = os.environ.get("GITHUB_BASE_REF", "").strip()
    if not base_ref:
        print("WARNING: DCO check runs on pull requests; no base ref is available, skipped.")
        return 0
    base = f"origin/{base_ref}"
    commits = subprocess.run(["git", "rev-list", f"{base}..HEAD"], check=True, text=True,
                             stdout=subprocess.PIPE).stdout.splitlines()
    failures = []
    for commit in commits:
        values = subprocess.run(["git", "show", "-s", "--format=%an%x00%ae%x00%B", commit], check=True,
                                text=True, stdout=subprocess.PIPE).stdout
        author_name, author_email, body = values.split("\0", 2)
        expected = f"Signed-off-by: {author_name.strip()} <{author_email.strip()}>"
        if expected not in body.splitlines():
            failures.append(commit[:12])
    if failures:
        print("DCO sign-off missing or does not match author on: " + ", ".join(failures))
        return 1
    print(f"DCO check passed for {len(commits)} commit(s).")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
