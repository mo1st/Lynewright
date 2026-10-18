#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Scan the working tree or Git history against an external private identity list."""
from __future__ import annotations
import argparse
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
LOCKFILE_NAMES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "poetry.lock", "uv.lock", "pipfile.lock", "cargo.lock", "composer.lock",
    "gemfile.lock", "go.sum",
}

def is_dependency_lockfile(value: str) -> bool:
    name = PurePosixPath(value).name.casefold()
    return name in LOCKFILE_NAMES or name.endswith(".lock")

def git(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(["git", "-C", str(ROOT), *args], input=input_bytes,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout

def load_terms(path: Path) -> list[tuple[str, bytes]]:
    rows = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        category, sep, value = raw.partition("\t")
        if not sep or not category.strip() or not value.strip():
            raise ValueError("Identity list rows must be category<TAB>literal.")
        rows.append((category.strip(), value.strip().encode("utf-8").lower()))
    if not rows:
        raise ValueError("Identity list has no scan terms.")
    return rows

def count(data: bytes, terms: list[tuple[str, bytes]]) -> dict[str, int]:
    haystack = data.lower()
    totals: dict[str, int] = {}
    for category, needle in terms:
        found = 0
        start = 0
        while True:
            position = haystack.find(needle, start)
            if position < 0:
                break
            found += 1
            start = position + max(1, len(needle))
        if found:
            totals[category] = totals.get(category, 0) + found
    return totals

def scan_tree(terms: list[tuple[str, bytes]]) -> dict[str, int]:
    rows = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").split(b"\0")
    totals: dict[str, int] = {}
    for raw_path in filter(None, rows):
        relative = raw_path.decode("utf-8", "surrogateescape")
        if is_dependency_lockfile(relative):
            continue
        path = ROOT / relative
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(ROOT.resolve())
            if not resolved.is_file() or resolved.stat().st_size > 20 * 1024 * 1024:
                continue
            hits = count(resolved.read_bytes(), terms)
        except (OSError, ValueError):
            continue
        for category, amount in hits.items():
            totals[category] = totals.get(category, 0) + amount
    return totals

def scan_history(terms: list[tuple[str, bytes]]) -> dict[str, int]:
    totals: dict[str, int] = {}
    metadata = git("log", "--all", "--format=%B%n%an%n%ae").lower()
    for category, amount in count(metadata, terms).items():
        totals[category] = totals.get(category, 0) + amount
    objects = git("rev-list", "--objects", "--all").decode("ascii", "replace").splitlines()
    object_ids = set()
    for line in objects:
        parts = line.split(" ", 1)
        if not parts or not parts[0]:
            continue
        object_path = parts[1] if len(parts) == 2 else ""
        if object_path and is_dependency_lockfile(object_path):
            continue
        object_ids.add(parts[0])
    object_ids = sorted(object_ids)
    if not object_ids:
        return totals
    checks = git("cat-file", "--batch-check=%(objectname) %(objecttype)",
                 input_bytes=("\n".join(object_ids) + "\n").encode("ascii")).decode("ascii", "replace").splitlines()
    blob_ids = [line.split(" ", 1)[0] for line in checks if line.endswith(" blob")]
    process = subprocess.Popen(["git", "-C", str(ROOT), "cat-file", "--batch"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None and process.stdout is not None
    for object_id in blob_ids:
        process.stdin.write(object_id.encode("ascii") + b"\n")
        process.stdin.flush()
        header = process.stdout.readline().split()
        if len(header) != 3 or header[1] != b"blob":
            raise RuntimeError("git cat-file returned an unexpected object")
        size = int(header[2])
        body = process.stdout.read(size)
        process.stdout.read(1)
        for category, amount in count(body, terms).items():
            totals[category] = totals.get(category, 0) + amount
    process.stdin.close()
    process.stdout.close()
    if process.wait() != 0:
        raise RuntimeError("git cat-file failed while scanning history")
    return totals

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--history", action="store_true", help="also scan reachable Git objects and commit metadata")
    args = parser.parse_args()
    raw_path = os.environ.get("LYNEWRIGHT_IDENTITY_SCAN_LIST", "").strip()
    if not raw_path:
        print("WARNING: LYNEWRIGHT_IDENTITY_SCAN_LIST is unset; identity scan skipped.")
        return 0
    try:
        terms = load_terms(Path(raw_path).expanduser())
        result = scan_tree(terms)
        label = "tree"
        if args.history:
            history = scan_history(terms)
            result = history
            label = "history"
    except (OSError, ValueError, subprocess.CalledProcessError, RuntimeError) as exc:
        print(f"Identity scan could not complete ({type(exc).__name__}).", file=sys.stderr)
        return 2
    categories = sorted({category for category, _ in terms})
    print(f"Identity scan ({label}):")
    total = sum(result.values())
    for category in categories:
        print(f"  {category}: {result.get(category, 0)}")
    print(f"  total: {total}")
    return 1 if total else 0

if __name__ == "__main__":
    raise SystemExit(main())
