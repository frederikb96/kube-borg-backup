"""Start gate for background pre-hooks and handling of failed background hooks."""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client

from common import hooks
from kube_pvc_snapshot import main as snap


class FakeStream:
    """Websocket stand-in that yields one stdout chunk per update() call."""

    def __init__(self, chunks: list[str], returncode: int = 0, on_chunk: Any = None) -> None:
        self.chunks = list(chunks)
        self.returncode = returncode
        self.on_chunk = on_chunk
        self._pending = ""

    def is_open(self) -> bool:
        return bool(self.chunks) or bool(self._pending)

    def update(self, timeout: int = 0) -> None:
        if self.chunks:
            self._pending = self.chunks.pop(0)

    def peek_stdout(self) -> bool:
        return bool(self._pending)

    def read_stdout(self) -> str:
        chunk, self._pending = self._pending, ""
        if self.on_chunk:
            self.on_chunk(chunk)
        return chunk

    def peek_stderr(self) -> bool:
        return False


def run_exec(monkeypatch: pytest.MonkeyPatch, resp: FakeStream, **kwargs: Any) -> dict[str, str]:
    monkeypatch.setattr(hooks, "stream", lambda *a, **k: resp)
    return hooks.execute_exec_hook(client.ApiClient(), "ns", "pod/db-0", ["sh"], **kwargs)


# -- marker detection in the exec stream -------------------------------------------------


def test_marker_sets_event_before_the_command_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    seen_while_running: list[bool] = []
    resp = FakeStream(
        ["pg_backup_start\n", "STARTED\n", "later output\n"],
        on_chunk=lambda chunk: seen_while_running.append(started.is_set()),
    )

    run_exec(monkeypatch, resp, started_marker="STARTED", started=started)

    assert started.is_set()
    assert seen_while_running == [False, False, True]


def test_marker_split_across_chunks_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()

    run_exec(monkeypatch, FakeStream(["STAR", "TED\n"]), started_marker="STARTED", started=started)

    assert started.is_set()


def test_no_marker_configured_never_sets_event(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()

    result = run_exec(monkeypatch, FakeStream(["STARTED\n"]), started=started)

    assert not started.is_set()
    assert result["stdout"] == "STARTED\n"


# -- the gate the controller waits on -----------------------------------------------------


def finished(exc: Exception | None = None) -> Future[Any]:
    future: Future[Any] = Future()
    if exc:
        future.set_exception(exc)
    else:
        future.set_result({})
    return future


def test_gate_opens_when_marker_seen() -> None:
    started = threading.Event()
    started.set()

    snap.wait_for_hook_start(started, Future(), "pre-hook-0", timeout=5)


def test_gate_times_out_without_marker() -> None:
    with pytest.raises(RuntimeError, match="did not signal"):
        snap.wait_for_hook_start(threading.Event(), Future(), "pre-hook-0", timeout=0)


def test_gate_fails_when_hook_dies_first() -> None:
    with pytest.raises(RuntimeError, match="bad credentials"):
        snap.wait_for_hook_start(threading.Event(), finished(Exception("bad credentials")), "pre-hook-0", timeout=5)


def test_gate_fails_when_hook_ends_without_marker() -> None:
    with pytest.raises(RuntimeError, match="without printing"):
        snap.wait_for_hook_start(threading.Event(), finished(), "pre-hook-0", timeout=5)


def test_gate_waits_for_a_marker_from_another_thread() -> None:
    started = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(lambda: (threading.Event().wait(0.2), started.set()))
        snap.wait_for_hook_start(started, future, "pre-hook-0", timeout=5)
    assert started.is_set()


# -- configuration of a gate ---------------------------------------------------------------


def test_marker_requires_a_timeout() -> None:
    with pytest.raises(ValueError, match="startedTimeoutSeconds"):
        snap.start_gate_timeout({"startedMarker": "X", "wait": False}, "pre-hook-0")


def test_marker_on_blocking_hook_is_rejected() -> None:
    with pytest.raises(ValueError, match="wait: false"):
        snap.start_gate_timeout({"startedMarker": "X", "startedTimeoutSeconds": 5}, "pre-hook-0")


def test_hook_without_marker_has_no_gate() -> None:
    assert snap.start_gate_timeout({"wait": False}, "pre-hook-0") is None


def test_gate_timeout_is_returned() -> None:
    hook = {"startedMarker": "X", "startedTimeoutSeconds": 20, "wait": False}
    assert snap.start_gate_timeout(hook, "pre-hook-0") == 20


# -- failed background hooks discard the cycle's snapshots ---------------------------------


def test_failed_background_hooks_are_reported() -> None:
    failures = snap.failed_background_hooks({"a": finished(), "b": finished(Exception("lock lost"))})

    assert failures == ["b: lock lost"]


class FakeSnapApi:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_namespaced_custom_object(self, group: str, version: str, ns: str, plural: str, name: str) -> Any:
        self.deleted.append(name)
        return SimpleNamespace()


def test_discard_snapshots_deletes_the_cycles_snapshots() -> None:
    api = FakeSnapApi()

    snap.discard_snapshots(api, ["a-snap-1", "b-snap-1"], "ns")  # type: ignore[arg-type]

    assert api.deleted == ["a-snap-1", "b-snap-1"]
