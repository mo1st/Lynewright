"""Synchronize marked documentation fences from repository files."""
from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
MARKER = re.compile(
    r"^<!-- snippet: (?P<path>[^#\r\n]+) -->\r?\n"
    r"(?:<!-- schema: (?P<schema>[a-z0-9-]+) -->\r?\n)?"
    r"```(?P<language>[A-Za-z0-9_-]+)\r?\n"
    r"(?P<body>.*?)\r?\n```",
    re.MULTILINE | re.DOTALL,
)


def sync_document(path: Path, *, write: bool) -> tuple[int, list[str]]:
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        source = (ROOT / match.group("path").strip()).resolve()
        try:
            source.relative_to(ROOT)
        except ValueError:
            errors.append(f"{path.relative_to(ROOT)}: snippet source escapes the repository: {match.group('path')}")
            return match.group(0)
        if not source.is_file():
            errors.append(f"{path.relative_to(ROOT)}: snippet source is missing: {match.group('path')}")
            return match.group(0)
        expected = source.read_text(encoding="utf-8").rstrip("\r\n")
        count += 1
        if match.group("body").rstrip("\r\n") == expected:
            return match.group(0)
        if not write:
            errors.append(f"{path.relative_to(ROOT)}: stale snippet from {match.group('path')}")
            return match.group(0)
        prefix = match.group(0).split(match.group("body"), 1)[0]
        return f"{prefix}{expected}\n```"

    updated = MARKER.sub(replace, text)
    if write and updated != text:
        path.write_text(updated, encoding="utf-8", newline="\n")
    return count, errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="rewrite marked fences from their source files")
    parser.add_argument("--check", action="store_true", help="check that marked fences match their source files (default)")
    args = parser.parse_args(argv)
    documents = [ROOT / "docs/integrations-howto.md", *sorted((ROOT / "docs/integrations").glob("*.md"))]
    total = 0
    errors: list[str] = []
    for document in documents:
        count, document_errors = sync_document(document, write=args.write)
        total += count
        errors.extend(document_errors)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    verb = "synchronized" if args.write else "current"
    print(f"{total} marked documentation snippet(s) {verb}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
