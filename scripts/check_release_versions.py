#!/usr/bin/env python3
"""Check that every file that writes the release version agrees, and that the CHANGELOG has its section.

    python scripts/check_release_versions.py          # the repository agrees with itself
    python scripts/check_release_versions.py v6.5.6   # ... and with this release tag

The version is written in the chart (`version` and `appVersion`), in `VERSION` of both images and
in `setup.py`, which `pipx` shows as the CLI's version. Every disagreement is printed, not just
the first; the exit code is non-zero when there is one.
"""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHART = ROOT / "charts" / "kube-borg-backup" / "Chart.yaml"
VERSION_FILES = [ROOT / "apps" / "controller" / "VERSION", ROOT / "apps" / "backup-runner" / "VERSION"]
SETUP = ROOT / "apps" / "setup.py"
CHANGELOG = ROOT / "CHANGELOG.md"

SEMVER = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")


def _match(pattern: str, path: Path) -> str | None:
    found = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    return found.group(1) if found else None


def collect(root: Path = ROOT) -> dict[str, str | None]:
    """Return every place that writes the version, with the value found (None when absent)."""
    chart = root / CHART.relative_to(ROOT)
    setup = root / SETUP.relative_to(ROOT)
    found: dict[str, str | None] = {
        "Chart.yaml version": _match(r"^version:\s*[\"']?([^\"'\s]+)", chart),
        "Chart.yaml appVersion": _match(r"^appVersion:\s*[\"']?([^\"'\s]+)", chart),
        "apps/setup.py version": _match(r"version=['\"]([^'\"]+)['\"]", setup),
    }
    for path in VERSION_FILES:
        version_file = root / path.relative_to(ROOT)
        found[str(path.relative_to(ROOT))] = version_file.read_text(encoding="utf-8").strip()
    return found


def problems(found: dict[str, str | None], changelog: str, tag: str | None) -> list[str]:
    """Return the disagreements between the version locations, the tag and the CHANGELOG."""
    issues = [f"{place} has no version" for place, value in found.items() if not value]
    values = {value for value in found.values() if value}
    if len(values) > 1:
        issues.append("version locations disagree: " + ", ".join(f"{p}={v}" for p, v in found.items()))
    for place, value in found.items():
        if value and not SEMVER.fullmatch(value):
            issues.append(f"{place} is not a semantic version: {value}")
    if len(values) == 1:
        version = next(iter(values))
        if tag is not None and tag.removeprefix("v") != version:
            issues.append(f"tag {tag} does not match version {version}")
        if f"## [{version}]" not in changelog:
            issues.append(f"CHANGELOG.md has no section for {version}")
    return issues


def main() -> int:
    """Print the versions found and every disagreement."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag", nargs="?", help="release tag, for example v6.5.6")
    args = parser.parse_args()

    found = collect()
    for place, value in found.items():
        print(f"{place:<28} {value}")
    issues = problems(found, CHANGELOG.read_text(encoding="utf-8"), args.tag)
    for issue in issues:
        print(f"error: {issue}", file=sys.stderr)
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
