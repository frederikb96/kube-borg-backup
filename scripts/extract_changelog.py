#!/usr/bin/env python3
"""Print the CHANGELOG section of one version, for use as the body of a GitHub Release.

    python scripts/extract_changelog.py 6.5.6

Writes the section body without its heading to stdout and exits non-zero when the version has no
section or the section is empty, so a release cannot publish with an empty body.
"""

import argparse
import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"

# "## [6.5.6] - 2026-11-07" and "## [Unreleased]"
HEADING = re.compile(r"^##\s+\[(?P<version>[^\]]+)\]")


def extract(text: str, version: str) -> str | None:
    """Return the body of the section for `version`, or None when there is none."""
    lines = text.splitlines()
    start: int | None = None
    end = len(lines)
    for index, line in enumerate(lines):
        match = HEADING.match(line)
        if not match:
            continue
        if start is None and match.group("version") == version:
            start = index + 1
        elif start is not None:
            end = index
            break
    if start is None:
        return None
    return "\n".join(lines[start:end]).strip()


def main() -> int:
    """Write the requested section to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version with or without a leading v")
    args = parser.parse_args()

    version = args.version.removeprefix("v")
    body = extract(CHANGELOG.read_text(encoding="utf-8"), version)
    if body is None:
        print(f"CHANGELOG.md has no section for {version}", file=sys.stderr)
        return 1
    if not body:
        print(f"CHANGELOG.md section for {version} is empty", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
