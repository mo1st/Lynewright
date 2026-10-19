#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Reject retired business/product vocabulary in the integration starter tree."""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BLOCKED = (
    "".join(("bel", "vio")), "".join(("k", "dp")), "".join(("epu", "bli")),
    "".join(("emo", " atlas")), "".join(("quor", "lyx")), "".join(("core", "lyx")), "".join(("grapes", "js")),
    "".join(("skin", " care")), "".join(("you", "cam")), "".join(("what", "sapp")),
    "".join(("money", "printer")), "".join(("woo", "commerce")), "".join(("shop", "ify")),
    "".join(("ve", "lith")),
)

def main() -> int:
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    violations = []
    paths = [ROOT / raw.decode("utf-8", "surrogateescape") for raw in result.stdout.split(b"\0") if raw]
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8").casefold()
        except (OSError, UnicodeError):
            continue
        for index, term in enumerate(BLOCKED, start=1):
            if term.casefold() in text:
                violations.append(f"{path.relative_to(ROOT).as_posix()}: retired-term-{index}")
    if violations:
        print("Business-word scan failed:\n" + "\n".join(violations), file=sys.stderr)
        return 1
    print(f"Business-word scan clean: {len(paths)} tracked paths.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
