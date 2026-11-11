"""The documentation tree: every docs file is linked from the README, every relative link resolves."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)|<img[^>]+src=\"([^\"]+)\"")
MARKDOWN = [ROOT / "README.md", *sorted((ROOT / "docs").glob("*.md"))]


def relative_targets(path: Path) -> list[str]:
    targets = [a or b for a, b in LINK.findall(path.read_text(encoding="utf-8"))]
    return [t.split("#")[0] for t in targets if not t.startswith(("http://", "https://", "#", "mailto:")) and t]


def test_every_docs_file_is_linked_from_the_readme() -> None:
    readme = relative_targets(ROOT / "README.md")
    orphans = [p.name for p in (ROOT / "docs").glob("*.md") if f"docs/{p.name}" not in readme]
    assert orphans == []


@pytest.mark.parametrize("path", MARKDOWN, ids=lambda p: str(p.relative_to(ROOT)))
def test_relative_links_resolve(path: Path) -> None:
    missing = [t for t in relative_targets(path) if not (path.parent / t).resolve().exists()]
    assert missing == []
