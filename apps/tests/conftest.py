"""Shared fixtures: import paths and a fake Kubernetes CoreV1Api."""

from __future__ import annotations

import sys
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from kubernetes import client
from kubernetes.client.rest import ApiException

APPS = Path(__file__).resolve().parents[1]
for sub in (APPS, APPS / "controller"):
    if str(sub) not in sys.path:
        sys.path.insert(0, str(sub))


class FakeCore:
    """In-memory stand-in for CoreV1Api covering the calls the controllers make.

    Objects are kept per kind in dicts keyed by name. ``errors[method]`` holds a queue of
    exceptions (or None for "succeed") consumed one per call to that method.
    """

    def __init__(self) -> None:
        self.pvcs: dict[str, Any] = {}
        self.secrets: dict[str, Any] = {}
        self.pods: dict[str, Any] = {}
        self.events: dict[str, list[Any]] = defaultdict(list)
        self.errors: dict[str, deque[Exception | None]] = defaultdict(deque)
        self.calls: list[tuple[str, str]] = []
        self.pvc_phases: dict[str, deque[str]] = {}
        self.pod_phases: dict[str, deque[str]] = {}

    def _maybe_fail(self, method: str) -> None:
        queue = self.errors[method]
        if queue:
            exc = queue.popleft()
            if exc is not None:
                raise exc

    # -- PVCs --------------------------------------------------------------------------
    def create_namespaced_persistent_volume_claim(self, namespace: str, body: dict[str, Any]) -> Any:
        self.calls.append(("create_pvc", body["metadata"]["name"]))
        self._maybe_fail("create_namespaced_persistent_volume_claim")
        self.pvcs[body["metadata"]["name"]] = body
        return body

    def read_namespaced_persistent_volume_claim(self, name: str, namespace: str) -> Any:
        self._maybe_fail("read_namespaced_persistent_volume_claim")
        if name not in self.pvcs:
            raise ApiException(status=404)
        phases = self.pvc_phases.get(name)
        phase = (phases.popleft() if len(phases) > 1 else phases[0]) if phases else "Bound"
        return SimpleNamespace(
            status=SimpleNamespace(phase=phase),
            spec=SimpleNamespace(volume_name=None),
        )

    def delete_namespaced_persistent_volume_claim(self, name: str, namespace: str) -> Any:
        self.calls.append(("delete_pvc", name))
        self._maybe_fail("delete_namespaced_persistent_volume_claim")
        if name not in self.pvcs:
            raise ApiException(status=404)
        del self.pvcs[name]

    def list_namespaced_persistent_volume_claim(self, namespace: str, **_: Any) -> Any:
        self._maybe_fail("list_namespaced_persistent_volume_claim")
        return SimpleNamespace(items=list(self.pvcs.values()))

    # -- secrets -----------------------------------------------------------------------
    def create_namespaced_secret(self, namespace: str, body: Any) -> Any:
        name = body["metadata"]["name"] if isinstance(body, dict) else body.metadata.name
        self.calls.append(("create_secret", name))
        self._maybe_fail("create_namespaced_secret")
        self.secrets[name] = body
        return body

    def delete_namespaced_secret(self, name: str, namespace: str) -> Any:
        self.calls.append(("delete_secret", name))
        self._maybe_fail("delete_namespaced_secret")
        if name not in self.secrets:
            raise ApiException(status=404)
        del self.secrets[name]

    def list_namespaced_secret(self, namespace: str, **_: Any) -> Any:
        self._maybe_fail("list_namespaced_secret")
        return SimpleNamespace(items=list(self.secrets.values()))

    # -- pods --------------------------------------------------------------------------
    def create_namespaced_pod(self, namespace: str, body: dict[str, Any]) -> Any:
        self.calls.append(("create_pod", body["metadata"]["name"]))
        self._maybe_fail("create_namespaced_pod")
        self.pods[body["metadata"]["name"]] = body
        return body

    def read_namespaced_pod(self, name: str, namespace: str) -> Any:
        self._maybe_fail("read_namespaced_pod")
        stored = self.pods.get(name)
        if stored is None:
            raise ApiException(status=404)
        if isinstance(stored, client.V1Pod):
            return stored
        phases = self.pod_phases.get(name)
        phase = (phases.popleft() if len(phases) > 1 else phases[0]) if phases else "Running"
        return SimpleNamespace(status=SimpleNamespace(phase=phase))

    def delete_namespaced_pod(self, name: str, namespace: str) -> Any:
        self.calls.append(("delete_pod", name))
        self._maybe_fail("delete_namespaced_pod")
        if name not in self.pods:
            raise ApiException(status=404)
        del self.pods[name]

    def list_namespaced_pod(self, namespace: str, **_: Any) -> Any:
        self._maybe_fail("list_namespaced_pod")
        return SimpleNamespace(items=[p for p in self.pods.values() if isinstance(p, client.V1Pod)])

    # -- events ------------------------------------------------------------------------
    def list_namespaced_event(self, namespace: str, field_selector: str = "", **_: Any) -> Any:
        name = field_selector.split("involvedObject.name=")[1].split(",")[0]
        return SimpleNamespace(items=list(self.events.get(name, [])))

    # -- seeding helpers ---------------------------------------------------------------
    def seed_pvc(self, name: str, labels: dict[str, str], age: timedelta, now: datetime) -> None:
        self.pvcs[name] = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=name, labels=labels, creation_timestamp=now - age)
        )

    def seed_secret(self, name: str, labels: dict[str, str], age: timedelta, now: datetime) -> None:
        self.secrets[name] = client.V1Secret(
            metadata=client.V1ObjectMeta(name=name, labels=labels, creation_timestamp=now - age)
        )

    def seed_pod(
        self,
        name: str,
        labels: dict[str, str],
        age: timedelta,
        now: datetime,
        phase: str = "Running",
        claims: tuple[str, ...] = (),
        secrets: tuple[str, ...] = (),
    ) -> None:
        volumes = [
            client.V1Volume(
                name=f"v{i}",
                persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(claim_name=c),
            )
            for i, c in enumerate(claims)
        ] + [
            client.V1Volume(name=f"s{i}", secret=client.V1SecretVolumeSource(secret_name=s))
            for i, s in enumerate(secrets)
        ]
        self.pods[name] = client.V1Pod(
            metadata=client.V1ObjectMeta(name=name, labels=labels, creation_timestamp=now - age),
            spec=client.V1PodSpec(containers=[], volumes=volumes),
            status=client.V1PodStatus(phase=phase),
        )


@pytest.fixture
def core() -> FakeCore:
    return FakeCore()


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neither the retry wrapper nor the poll loops may really sleep."""
    import time

    monkeypatch.setattr(time, "sleep", lambda _s: None)
