"""Owner references, ephemeral labels and the start-of-run sweep."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

from kube_snapshot_borgbackup import main as bb

NS = "app"
RELEASE = "kbb-prod-app"
MANAGED = {"app": "kube-borg-backup", "managed-by": "kube-borg-backup"}
EPHEMERAL = {**MANAGED, "ephemeral": "true"}
HOUR = timedelta(hours=1)
LIMIT = 3600


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in bb._tracked_resources:
        bb._tracked_resources[key].clear()
    monkeypatch.setattr(bb, "_owner_references", [], raising=False)


JOB_OWNER = {
    "apiVersion": "batch/v1",
    "kind": "Job",
    "name": "kbb-prod-app-borgbackup-123",
    "uid": "uid-1",
    "blockOwnerDeletion": False,
    "controller": False,
}


# -- ownerReference resolution -----------------------------------------------------------


def controller_pod(owners: list[client.V1OwnerReference] | None) -> client.V1Pod:
    return client.V1Pod(metadata=client.V1ObjectMeta(name="ctl", owner_references=owners))


def job_ref() -> client.V1OwnerReference:
    return client.V1OwnerReference(
        api_version="batch/v1", kind="Job", name="kbb-prod-app-borgbackup-123", uid="uid-1", controller=True,
        block_owner_deletion=True,
    )


def test_owner_reference_points_at_the_controllers_job(core: Any) -> None:
    core.pods["ctl"] = controller_pod([client.V1OwnerReference(
        api_version="v1", kind="Other", name="x", uid="u"), job_ref()])

    assert bb.resolve_owner_references(core, NS, "ctl") == [JOB_OWNER]


def test_no_owner_reference_without_a_job_owner(core: Any) -> None:
    core.pods["ctl"] = controller_pod(None)

    assert bb.resolve_owner_references(core, NS, "ctl") == []


def test_no_owner_reference_outside_a_pod(core: Any) -> None:
    assert bb.resolve_owner_references(core, NS, None) == []


def test_owner_reference_lookup_failure_is_not_fatal(core: Any) -> None:
    assert bb.resolve_owner_references(core, NS, "absent") == []


# -- ephemeral objects carry labels and owner ----------------------------------------------


def test_clone_pvc_carries_owner_and_ephemeral_label(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "_owner_references", [JOB_OWNER])
    snap_api = type("Snap", (), {"get_namespaced_custom_object": lambda *a, **k: {"status": {}}})()

    bb.create_clone_pvc(core, snap_api, "snap", "snap-clone-1", "sc", NS)

    meta = core.pvcs["snap-clone-1"]["metadata"]
    assert meta["ownerReferences"] == [JOB_OWNER]
    assert meta["labels"]["ephemeral"] == "true"
    assert meta["labels"]["managed-by"] == "kube-borg-backup"


def test_secret_carries_owner(core: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "_owner_references", [JOB_OWNER])

    bb.create_borg_secret(core, "s", "repo", "pw", "key", {}, "data", "/data", 60, False, [], NS)

    meta = core.secrets["s"]["metadata"]
    assert meta["ownerReferences"] == [JOB_OWNER]
    assert meta["labels"]["ephemeral"] == "true"


def test_pod_manifest_carries_owner_and_ephemeral_label(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bb, "_owner_references", [JOB_OWNER])

    manifest = bb.build_borg_pod_manifest("p", "data", "clone", {}, "secret", "cache", 60, NS)

    assert manifest["metadata"]["ownerReferences"] == [JOB_OWNER]
    assert manifest["metadata"]["labels"]["ephemeral"] == "true"


def test_no_owner_key_when_unresolved() -> None:
    manifest = bb.build_borg_pod_manifest("p", "data", "clone", {}, "secret", "cache", 60, NS)

    assert "ownerReferences" not in manifest["metadata"]


# -- sweep age limit ----------------------------------------------------------------------


def test_sweep_age_limit_covers_a_whole_sequential_run() -> None:
    backups = [
        {"timeout": 1000, "cloneBindTimeout": 100},
        {"timeout": 2000},
    ]

    assert bb.sweep_age_limit(backups) == 1000 + 100 + 2000 + 300


# -- sweep selection -----------------------------------------------------------------------


def sweep(core: Any, now: Any, pvcs: tuple[str, ...] = ("data",)) -> dict[str, list[str]]:
    return bb.sweep_stale_resources(core, NS, RELEASE, list(pvcs), LIMIT, now=now)


def test_sweep_deletes_old_unused_leftovers(core: Any, now: Any) -> None:
    core.seed_pvc("data-snap-1-clone-1", MANAGED, 2 * HOUR, now)
    core.seed_secret(f"{RELEASE}-backup-runner-data-1-config", EPHEMERAL, 2 * HOUR, now)
    core.seed_pod(f"{RELEASE}-backup-runner-data-1", EPHEMERAL, 2 * HOUR, now, phase="Failed")

    result = sweep(core, now)

    assert result == {
        "pvcs": ["data-snap-1-clone-1"],
        "secrets": [f"{RELEASE}-backup-runner-data-1-config"],
        "pods": [f"{RELEASE}-backup-runner-data-1"],
    }
    assert not core.pvcs and not core.secrets and not core.pods


def test_sweep_keeps_young_resources(core: Any, now: Any) -> None:
    core.seed_pvc("data-snap-1-clone-1", MANAGED, HOUR // 2, now)
    core.seed_secret(f"{RELEASE}-backup-runner-data-1-config", EPHEMERAL, HOUR // 2, now)

    assert sweep(core, now) == {"pvcs": [], "secrets": [], "pods": []}
    assert core.pvcs and core.secrets


def test_sweep_keeps_pvc_and_secret_used_by_a_live_pod(core: Any, now: Any) -> None:
    core.seed_pvc("data-snap-1-clone-1", MANAGED, 2 * HOUR, now)
    core.seed_secret(f"{RELEASE}-backup-runner-data-1-config", EPHEMERAL, 2 * HOUR, now)
    core.seed_pod(
        "someone-else", {}, HOUR // 4, now,
        claims=("data-snap-1-clone-1",), secrets=(f"{RELEASE}-backup-runner-data-1-config",),
    )

    assert sweep(core, now) == {"pvcs": [], "secrets": [], "pods": []}


def test_sweep_ignores_a_terminal_pod_that_still_references_them(core: Any, now: Any) -> None:
    core.seed_pvc("data-snap-1-clone-1", MANAGED, 2 * HOUR, now)
    core.seed_pod("done", {}, 2 * HOUR, now, phase="Succeeded", claims=("data-snap-1-clone-1",))

    assert sweep(core, now)["pvcs"] == ["data-snap-1-clone-1"]


def test_sweep_never_touches_unlabelled_or_foreign_objects(core: Any, now: Any) -> None:
    core.seed_pvc("borg-cache", {"app.kubernetes.io/managed-by": "Helm"}, 99 * HOUR, now)
    core.seed_pvc("data-snap-1-clone-1-lookalike", {}, 99 * HOUR, now)
    core.seed_pvc("other-snap-1-clone-1", MANAGED, 99 * HOUR, now)  # another app's PVC
    core.seed_pvc("data-snap-1-not-a-copy", MANAGED, 99 * HOUR, now)
    core.seed_secret("unrelated", {}, 99 * HOUR, now)
    core.seed_secret("other-release-backup-runner-x-config", EPHEMERAL, 99 * HOUR, now)
    core.seed_pod("web", {}, 99 * HOUR, now)

    assert sweep(core, now) == {"pvcs": [], "secrets": [], "pods": []}
    assert len(core.pvcs) == 4 and len(core.secrets) == 2 and len(core.pods) == 1


def test_sweep_survives_listing_errors(core: Any, now: Any, capsys: pytest.CaptureFixture[str]) -> None:
    core.errors["list_namespaced_pod"].append(ApiException(status=403))

    result = bb.sweep_stale_resources(core, NS, RELEASE, ["data"], LIMIT, now=now)

    assert result == {"pvcs": [], "secrets": [], "pods": []}
    assert "403" in capsys.readouterr().out
