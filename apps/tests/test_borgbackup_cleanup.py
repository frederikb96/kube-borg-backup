"""Cleanup, polling and tracking behaviour of the borgbackup controller."""

from __future__ import annotations

from collections import deque
from typing import Any

import pytest
from kubernetes.client.rest import ApiException

from kube_snapshot_borgbackup import main as bb

NS = "app"


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in bb._tracked_resources:
        bb._tracked_resources[key].clear()
    bb._failures.clear()
    monkeypatch.setattr(bb, "_owner_references", [], raising=False)


class FakeMonitor:
    def __init__(self, *_: Any) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> None: ...


def make_clone(name: str = "data-snap-1-clone-1", **cfg: Any) -> bb.ClonePVC:
    backup_config = {"name": "data", "pvc": "data", "class": "sc", "timeout": 60, "cloneBindTimeout": 5, **cfg}
    return bb.ClonePVC("data", "data", name, "data-snap-1", backup_config)


def run_backup(core: Any, clone: bb.ClonePVC) -> bool:
    return bb.process_backup_with_clone(
        clone, core, "rel", {}, "repo", "pass", "key", "cache", False, [], {}, NS, False
    )


def track_clone(core: Any, name: str) -> None:
    core.pvcs[name] = {"metadata": {"name": name}}
    bb._tracked_resources["clone_pvcs"].append(name)


# -- every exit path deletes the clone, secret and pod -----------------------------------


def test_clone_deleted_when_wait_for_bind_fails(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = make_clone()
    track_clone(core, clone.clone_name)
    monkeypatch.setattr(bb, "wait_clone_pvc_ready", lambda *a, **k: (False, "boom"))

    assert run_backup(core, clone) is False

    assert clone.clone_name not in core.pvcs


def test_clone_deleted_when_timeout_missing(core: Any) -> None:
    clone = make_clone()
    clone.backup_config.pop("timeout")
    track_clone(core, clone.clone_name)

    assert run_backup(core, clone) is False

    assert clone.clone_name not in core.pvcs


def test_everything_deleted_when_borg_pod_fails(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = make_clone()
    track_clone(core, clone.clone_name)
    monkeypatch.setattr(bb, "wait_clone_pvc_ready", lambda *a, **k: (True, ""))

    def fake_spawn(v1: Any, manifest: dict[str, Any], namespace: str, timeout: int) -> bool:
        v1.create_namespaced_pod(namespace, manifest)
        bb._tracked_resources["borg_pods"].append(manifest["metadata"]["name"])
        return False

    monkeypatch.setattr(bb, "spawn_borg_pod", fake_spawn)

    assert run_backup(core, clone) is False

    assert not core.pvcs and not core.secrets and not core.pods
    assert not any(bb._tracked_resources.values())


def test_everything_deleted_when_pod_creation_raises(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    clone = make_clone()
    track_clone(core, clone.clone_name)
    monkeypatch.setattr(bb, "wait_clone_pvc_ready", lambda *a, **k: (True, ""))

    def boom(*_: Any, **__: Any) -> bool:
        raise RuntimeError("spawn exploded")

    monkeypatch.setattr(bb, "spawn_borg_pod", boom)

    assert run_backup(core, clone) is False

    assert not core.pvcs and not core.secrets


def test_delete_tracked_resources_removes_everything_left(core: Any) -> None:
    track_clone(core, "c1")
    core.secrets["s1"] = {}
    core.pods["p1"] = {}
    bb._tracked_resources["ssh_secrets"].append("s1")
    bb._tracked_resources["borg_pods"].append("p1")

    bb.delete_tracked_resources(core, NS)

    assert not core.pvcs and not core.secrets and not core.pods
    assert not any(bb._tracked_resources.values())


# -- delete helpers retry, log and treat 404 as done -------------------------------------


def test_delete_retries_transient_error(core: Any) -> None:
    track_clone(core, "c1")
    core.errors["delete_namespaced_persistent_volume_claim"] = deque([ApiException(status=500), None])

    bb.delete_pvc(core, "c1", NS)

    assert "c1" not in core.pvcs
    assert "c1" not in bb._tracked_resources["clone_pvcs"]


def test_delete_failure_is_logged_and_stays_tracked(core: Any, capsys: pytest.CaptureFixture[str]) -> None:
    track_clone(core, "c1")
    core.errors["delete_namespaced_persistent_volume_claim"] = deque([ApiException(status=403)])

    bb.delete_pvc(core, "c1", NS)

    assert "c1" in bb._tracked_resources["clone_pvcs"]
    assert "c1" in capsys.readouterr().out


def test_delete_of_missing_object_counts_as_done(core: Any) -> None:
    bb._tracked_resources["ssh_secrets"].append("gone")

    bb.delete_secret(core, "gone", NS)

    assert "gone" not in bb._tracked_resources["ssh_secrets"]


# -- clone is registered before the create call ------------------------------------------


def test_clone_tracked_even_when_create_never_returns(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    snap_api = type("Snap", (), {"get_namespaced_custom_object": lambda *a, **k: {"status": {}}})()
    core.errors["create_namespaced_persistent_volume_claim"] = deque([ApiException(status=500)] * 4)

    with pytest.raises(ApiException):
        bb.create_clone_pvc(core, snap_api, "snap", "snap-clone-1", "sc", NS)

    assert "snap-clone-1" in bb._tracked_resources["clone_pvcs"]


# -- waiting for the clone tolerates transient trouble -----------------------------------


def warning_event(message: str) -> Any:
    from kubernetes import client

    return client.CoreV1Event(
        metadata=client.V1ObjectMeta(name="e"),
        involved_object=client.V1ObjectReference(),
        type="Warning",
        message=message,
    )


def test_single_warning_event_does_not_fail_the_wait(core: Any) -> None:
    core.pvcs["c1"] = {}
    core.pvc_phases["c1"] = deque(["Pending", "Pending", "Bound"])
    core.events["c1"] = [warning_event("failed to provision volume: transient snapshot lookup error")]

    ok, error = bb.wait_clone_pvc_ready(core, "c1", NS, timeout=60)

    assert ok, error


def test_api_error_while_waiting_for_bind_is_retried(core: Any) -> None:
    core.pvcs["c1"] = {}
    core.errors["read_namespaced_persistent_volume_claim"] = deque([ApiException(status=500), None])

    ok, error = bb.wait_clone_pvc_ready(core, "c1", NS, timeout=60)

    assert ok, error


def test_missing_clone_fails_the_wait(core: Any) -> None:
    ok, error = bb.wait_clone_pvc_ready(core, "absent", NS, timeout=60)

    assert not ok
    assert "404" in error


def test_timeout_reports_the_warning_event(core: Any) -> None:
    core.pvcs["c1"] = {}
    core.pvc_phases["c1"] = deque(["Pending"])
    core.events["c1"] = [warning_event("ProvisioningFailed: no space")]

    ok, error = bb.wait_clone_pvc_ready(core, "c1", NS, timeout=0)

    assert not ok
    assert "no space" in error


# -- polling a running borg pod ----------------------------------------------------------


def pod_manifest(name: str = "p1") -> dict[str, Any]:
    return {"metadata": {"name": name}}


def test_api_error_during_pod_poll_is_retried(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "PodMonitor", FakeMonitor)
    core.pod_phases["p1"] = deque(["Succeeded"])
    original_create = core.create_namespaced_pod

    def create_and_arm(namespace: str, body: dict[str, Any]) -> Any:
        result = original_create(namespace, body)
        core.errors["read_namespaced_pod"] = deque([ApiException(status=500), ApiException(status=503), None])
        return result

    core.create_namespaced_pod = create_and_arm

    assert bb.spawn_borg_pod(core, pod_manifest(), NS, timeout=600) is True


def test_pod_poll_still_fails_on_failed_phase(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "PodMonitor", FakeMonitor)
    core.pod_phases["p1"] = deque(["Running", "Failed"])

    assert bb.spawn_borg_pod(core, pod_manifest(), NS, timeout=600) is False
