"""The environment the runner hands to borg."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

COMMON = Path(__file__).resolve().parents[1] / "backup-runner" / "common.py"


def load_common():
    spec = importlib.util.spec_from_file_location("runner_common", COMMON)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CONFIG = {"borgRepo": "ssh://example/./repo", "borgPassphrase": "x"}


def test_files_cache_ttl_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BORG_FILES_CACHE_TTL", raising=False)
    common = load_common()
    env = common.get_borg_env(CONFIG, "/tmp/key")
    assert int(env["BORG_FILES_CACHE_TTL"]) >= 1000


def test_files_cache_ttl_from_pod_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BORG_FILES_CACHE_TTL", "50")
    common = load_common()
    env = common.get_borg_env(CONFIG, "/tmp/key")
    assert env["BORG_FILES_CACHE_TTL"] == "50"
