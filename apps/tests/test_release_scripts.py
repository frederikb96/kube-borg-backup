"""The release scripts: version agreement and the CHANGELOG section a GitHub Release is cut from."""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


versions = load("check_release_versions")
changelog = load("extract_changelog")


def test_repository_versions_agree() -> None:
    found = versions.collect(ROOT)
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert versions.problems(found, text, None) == []


def test_disagreeing_locations_are_all_reported(tmp_path: Path) -> None:
    for relative in ("charts/kube-borg-backup/Chart.yaml", "apps/setup.py",
                     "apps/controller/VERSION", "apps/backup-runner/VERSION"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)
    (tmp_path / "apps/controller/VERSION").write_text("1.0.0\n", encoding="utf-8")
    setup = tmp_path / "apps/setup.py"
    setup.write_text(setup.read_text(encoding="utf-8").replace("version='", "version='9", 1), encoding="utf-8")

    issues = versions.problems(versions.collect(tmp_path), "", None)

    assert len(issues) == 1
    assert "apps/controller/VERSION=1.0.0" in issues[0]
    assert "apps/setup.py version=9" in issues[0]


def test_tag_must_match_the_version() -> None:
    found = dict.fromkeys(["a", "b"], "1.2.3")
    text = "## [1.2.3] - 2026-01-01"
    assert versions.problems(found, text, "v1.2.3") == []
    assert versions.problems(found, text, "v1.2.4") == ["tag v1.2.4 does not match version 1.2.3"]


def test_version_needs_a_changelog_section() -> None:
    found = dict.fromkeys(["a"], "1.2.3")
    assert versions.problems(found, "## [Unreleased]", None) == ["CHANGELOG.md has no section for 1.2.3"]


def test_missing_version_is_reported() -> None:
    assert versions.problems({"a": None, "b": "1.0.0"}, "## [1.0.0]", None) == ["a has no version"]


SAMPLE = """# Changelog

## [Unreleased]

## [1.1.0] - 2026-02-01

### Added
- one

## [1.0.0] - 2026-01-01

### Fixed
- two
"""


def test_extract_returns_only_the_requested_section() -> None:
    assert changelog.extract(SAMPLE, "1.1.0") == "### Added\n- one"
    assert changelog.extract(SAMPLE, "1.0.0") == "### Fixed\n- two"


def test_extract_without_section_is_none() -> None:
    assert changelog.extract(SAMPLE, "2.0.0") is None


def test_extract_of_empty_section_is_empty() -> None:
    assert changelog.extract(SAMPLE, "Unreleased") == ""


def test_current_release_has_notes() -> None:
    version = versions.collect(ROOT)["Chart.yaml version"]
    assert version
    assert changelog.extract((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"), version)
