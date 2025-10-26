"""Restore snapshots to temporary PVCs and run borg backups.

This module orchestrates the BorgBackup process:
1. Create clone PVCs from VolumeSnapshots (parallel for speed)
2. Execute backups sequentially (borg repo only supports one writer)
3. Clean up temporary resources (always, even on SIGTERM)

The controller uses an optimized two-phase approach:
- Phase 1: Start ALL clone PVC creation in parallel (non-blocking)
- Phase 2: Process backups SEQUENTIALLY, waiting for each clone individually

This maximizes parallelism - while backup N runs, clones N+1, N+2, etc. continue
provisioning in the background. First backup starts as soon as first clone is ready.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config as k8s_config, watch
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

SNAP_GROUP = "snapshot.storage.k8s.io"
SNAP_VERSION = "v1"
SNAP_PLURAL = "volumesnapshots"

# Global state for SIGTERM handler
_tracked_resources: dict[str, list[str]] = {"clone_pvcs": [], "borg_pods": [], "ssh_secrets": []}
_namespace: str | None = None
_core_api: client.CoreV1Api | None = None
_storage_api: client.StorageV1Api | None = None
_failures: list[str] = []


@dataclass
class ClonePVC:
    """Represents a clone PVC and its associated backup configuration."""
    backup_name: str
    pvc_name: str
    clone_name: str
    snapshot_name: str
    backup_config: dict[str, Any]
    failed: bool = False
    failure_reason: str | None = None


def log_msg(msg: str) -> None:
    """Log message to stdout with consistent formatting."""
    print(msg)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Run borg backups from PVC snapshots")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("--test", action="store_true", help="Test mode: skip borg pod spawn")
    return parser.parse_args()


def resolve_config_path(cli_path: str | None) -> Path:
    """Resolve the config file path from CLI, env, or default."""
    if cli_path:
        return Path(cli_path)
    env_path = os.getenv("APP_CONFIG")
    if env_path:
        return Path(env_path)
    return Path("/config/config.yaml")


def load_config(cli_path: str | None) -> dict[str, Any]:
    """Load and validate configuration from YAML file."""
    path = resolve_config_path(cli_path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        log_msg(f"❌ Config file not found: {path}")
        sys.exit(2)
    except Exception as exc:
        log_msg(f"❌ Failed to read config {path}: {exc}")
        sys.exit(2)
    if not isinstance(data, dict):
        log_msg("❌ Config root must be a mapping")
        sys.exit(2)
    return data


def init_clients() -> tuple[client.CoreV1Api, client.CustomObjectsApi, client.StorageV1Api]:
    """Initialize Kubernetes API clients."""
    try:
        k8s_config.load_incluster_config()
    except ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:
            log_msg(f"❌ Failed to load kubeconfig: {exc}")
            sys.exit(3)
    return client.CoreV1Api(), client.CustomObjectsApi(), client.StorageV1Api()


def cleanup_all_resources() -> None:
    """SIGTERM handler: Clean up all tracked resources."""
    if not (_core_api and _namespace):
        return

    log_msg("\n\n🛑 Received SIGTERM - cleaning up all tracked resources...")

    # Clean up config secrets
    for secret_name in _tracked_resources["ssh_secrets"]:
        try:
            log_msg(f"🗑️  Deleting config secret: {secret_name}")
            _core_api.delete_namespaced_secret(secret_name, _namespace)
        except ApiException as exc:
            log_msg(f"⚠️  Failed to delete secret {secret_name}: {exc}")

    # Clean up borg pods
    for pod_name in _tracked_resources["borg_pods"]:
        try:
            log_msg(f"🗑️  Deleting borg pod: {pod_name}")
            _core_api.delete_namespaced_pod(pod_name, _namespace)
        except ApiException as exc:
            log_msg(f"⚠️  Failed to delete pod {pod_name}: {exc}")

    # Clean up clone PVCs
    for pvc_name in _tracked_resources["clone_pvcs"]:
        try:
            log_msg(f"🗑️  Deleting clone PVC: {pvc_name}")
            _core_api.delete_namespaced_persistent_volume_claim(pvc_name, _namespace)
        except ApiException as exc:
            log_msg(f"⚠️  Failed to delete PVC {pvc_name}: {exc}")

    log_msg("✅ Cleanup complete")
    sys.exit(143)  # Standard exit code for SIGTERM


def validate_storage_class(storage_api: client.StorageV1Api, storage_class: str) -> tuple[bool, str]:
    """Validate that a storage class exists.

    Args:
        storage_api: StorageV1Api client
        storage_class: Storage class name to validate

    Returns:
        Tuple of (exists: bool, error_message: str or empty)
    """
    try:
        storage_api.read_storage_class(storage_class)
        return True, ""
    except ApiException as exc:
        if exc.status == 404:
            return False, f"Storage class '{storage_class}' not found"
        return False, f"Failed to validate storage class '{storage_class}': {exc}"
    except Exception as exc:
        return False, f"Unexpected error validating storage class '{storage_class}': {exc}"


def latest_snapshot(
    snap_api: client.CustomObjectsApi,
    pvc: str,
    namespace: str
) -> str | None:
    """Find the latest ready snapshot for a PVC.

    Args:
        snap_api: CustomObjectsApi client
        pvc: PVC name to find snapshot for
        namespace: Kubernetes namespace

    Returns:
        Snapshot name, or None if not found
    """
    try:
        snaps = snap_api.list_namespaced_custom_object(
            SNAP_GROUP, SNAP_VERSION, namespace, SNAP_PLURAL,
            label_selector=f"pvc={pvc}"
        )
        items = [s for s in snaps.get("items", []) if s.get("status", {}).get("readyToUse")]
        items.sort(key=lambda s: s.get("metadata", {}).get("creationTimestamp", ""))
        if not items:
            return None
        return items[-1]["metadata"]["name"]
    except ApiException as exc:
        log_msg(f"❌ Failed to list snapshots for {pvc}: {exc}")
        return None


def create_clone_pvc(
    v1: client.CoreV1Api,
    snap_api: client.CustomObjectsApi,
    snap_name: str,
    clone_name: str,
    storage_class: str,
    namespace: str
) -> None:
    """Create a clone PVC from a VolumeSnapshot.

    Args:
        v1: CoreV1Api client
        snap_api: CustomObjectsApi client
        snap_name: VolumeSnapshot name to clone from
        clone_name: Name for the clone PVC
        storage_class: Storage class for the clone
        namespace: Kubernetes namespace

    Raises:
        ApiException: If clone creation fails
    """
    snap = snap_api.get_namespaced_custom_object(SNAP_GROUP, SNAP_VERSION, namespace, SNAP_PLURAL, snap_name)
    size = snap.get("status", {}).get("restoreSize", "1Gi")

    body = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": clone_name,
            "namespace": namespace,
            "labels": {
                "app": "kube-borg-backup",
                "pvc": clone_name,
                "managed-by": "kube-borg-backup"
            }
        },
        "spec": {
            "accessModes": ["ReadWriteOncePod"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
            "dataSource": {
                "name": snap_name,
                "kind": "VolumeSnapshot",
                "apiGroup": SNAP_GROUP,
            },
        },
    }

    v1.create_namespaced_persistent_volume_claim(namespace, body)
    _tracked_resources["clone_pvcs"].append(clone_name)


def create_borg_secret(
    v1: client.CoreV1Api,
    secret_name: str,
    borg_repo: str,
    borg_passphrase: str,
    ssh_key: str,
    retention: dict[str, int],
    backup_name: str,
    backup_dir: str,
    lock_wait: int,
    namespace: str
) -> None:
    """Create ephemeral secret with borg configuration file.

    Args:
        v1: CoreV1Api client
        secret_name: Name for the secret
        borg_repo: Borg repository URL
        borg_passphrase: Borg passphrase
        ssh_key: SSH private key content
        retention: Retention policy (hourly, daily, weekly, monthly, yearly)
        backup_name: Backup identifier (archive prefix)
        backup_dir: Directory to backup
        lock_wait: Lock wait timeout in seconds
        namespace: Kubernetes namespace

    Raises:
        ApiException: If secret creation fails
    """
    # Build config dictionary
    config = {
        "borgRepo": borg_repo,
        "borgPassphrase": borg_passphrase,
        "sshPrivateKey": ssh_key,
        "prefix": backup_name,
        "backupDir": backup_dir,
        "lockWait": lock_wait,
    }

    # Add retention if specified
    if retention:
        config["retention"] = {
            k: v for k, v in retention.items() if v is not None
        }

    # Serialize to YAML
    config_yaml = yaml.dump(config, default_flow_style=False, sort_keys=False)

    body = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=secret_name,
            namespace=namespace,
            labels={
                "app": "kube-borg-backup",
                "managed-by": "kube-borg-backup",
                "ephemeral": "true"
            }
        ),
        type="Opaque",
        string_data={
            "config.yaml": config_yaml
        }
    )

    v1.create_namespaced_secret(namespace, body)
    _tracked_resources["ssh_secrets"].append(secret_name)


def wait_clone_pvc_ready(
    v1: client.CoreV1Api,
    pvc_name: str,
    namespace: str,
    timeout: int = 300
) -> tuple[bool, str]:
    """Wait for clone PVC to be Bound or WaitForFirstConsumer.

    Handles both Immediate and WaitForFirstConsumer storage classes.
    For WaitForFirstConsumer, the PVC won't bind until a pod uses it.

    Args:
        v1: CoreV1Api client
        pvc_name: Name of PVC to wait for
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        Tuple of (success: bool, error_message: str or empty)
    """
    start_time = time.time()
    last_event_check = 0.0

    while True:
        elapsed = int(time.time() - start_time)

        # Check timeout
        if elapsed >= timeout:
            # Final event check to surface actual error
            error_msg = _check_pvc_events_for_errors(v1, pvc_name, namespace)
            if error_msg:
                log_msg(f"❌ PVC {pvc_name} provisioning failed: {error_msg}")
                return False, error_msg
            log_msg(f"⏰ Timeout waiting for PVC {pvc_name} after {elapsed}s")
            return False, f"Timeout after {elapsed}s"

        try:
            # Get PVC status
            pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
            status = pvc.status.phase

            # Check if Bound
            if status == "Bound":
                log_msg(f"✅ PVC {pvc_name} is Bound after {elapsed}s")
                return True, ""

            # Check if WaitForFirstConsumer (ready to be used by pod)
            if status == "Pending":
                # Check events every 10 seconds to detect errors early
                current_time = time.time()
                if current_time - last_event_check >= 10:
                    last_event_check = current_time
                    error_msg = _check_pvc_events_for_errors(v1, pvc_name, namespace)
                    if error_msg:
                        log_msg(f"❌ PVC {pvc_name} provisioning failed: {error_msg}")
                        return False, error_msg

                    # Check if WaitForFirstConsumer
                    events = v1.list_namespaced_event(
                        namespace,
                        field_selector=f"involvedObject.name={pvc_name},involvedObject.kind=PersistentVolumeClaim"
                    )
                    for event in events.items:
                        if "WaitForFirstConsumer" in event.message or "waiting for first consumer" in event.message:
                            log_msg(f"🕓 PVC {pvc_name} waiting for first consumer after {elapsed}s - ready to use")
                            return True, ""

        except ApiException as exc:
            log_msg(f"⚠️ Error checking PVC {pvc_name}: {exc}")
            return False, str(exc)

        time.sleep(5)


def _check_pvc_events_for_errors(
    v1: client.CoreV1Api,
    pvc_name: str,
    namespace: str
) -> str:
    """Check PVC events for provisioning errors.

    Args:
        v1: CoreV1Api client
        pvc_name: PVC name to check events for
        namespace: Kubernetes namespace

    Returns:
        Error message if found, empty string otherwise
    """
    try:
        events = v1.list_namespaced_event(
            namespace,
            field_selector=f"involvedObject.name={pvc_name},involvedObject.kind=PersistentVolumeClaim"
        )

        # Look for error/warning events
        error_keywords = [
            "ProvisioningFailed",
            "not found",
            "failed",
            "error",
            "cannot",
            "unable"
        ]

        for event in events.items:
            if event.type in ["Warning", "Error"]:
                message = event.message.lower()
                if any(keyword in message for keyword in error_keywords):
                    return event.message

        return ""
    except ApiException:
        return ""


def build_borg_pod_manifest(
    pod_name: str,
    backup_name: str,
    clone_pvc: str,
    pod_config: dict[str, Any],
    config_secret: str,
    cache_pvc: str,
    pvc_timeout: int,
    namespace: str
) -> dict[str, Any]:
    """Build borg pod manifest as pure Python dict.

    Args:
        pod_name: Name for the borg pod
        backup_name: Backup identifier (archive prefix)
        clone_pvc: Name of clone PVC to mount
        pod_config: Pod configuration (image, resources)
        config_secret: Name of ephemeral secret containing config.yaml
        cache_pvc: Name of borg cache PVC
        pvc_timeout: Per-PVC timeout (pod activeDeadlineSeconds)
        namespace: Kubernetes namespace

    Returns:
        Pod manifest as dict
    """
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app": "kube-borg-backup",
                "backup": backup_name,
                "managed-by": "kube-borg-backup"
            }
        },
        "spec": {
            "activeDeadlineSeconds": pvc_timeout,
            "restartPolicy": "Never",
            "containers": [
                {
                    "name": "borg",
                    "image": (
                        f"{pod_config.get('image', {}).get('repository', 'ghcr.io/frederikb96/kube-borg-backup/backup-runner')}"  # noqa: E501
                        f":{pod_config.get('image', {}).get('tag', 'latest')}"
                    ),
                    "imagePullPolicy": pod_config.get("image", {}).get("pullPolicy", "IfNotPresent"),
                    "securityContext": {
                        "privileged": pod_config.get("privileged", True)
                    },
                    "volumeMounts": [
                        {
                            "name": "config",
                            "mountPath": "/config",
                            "readOnly": True
                        },
                        {
                            "name": "data",
                            "mountPath": "/data",
                            "readOnly": True
                        },
                        {
                            "name": "cache",
                            "mountPath": "/cache"
                        }
                    ],
                    "resources": pod_config.get("resources", {})
                }
            ],
            "volumes": [
                {
                    "name": "config",
                    "secret": {
                        "secretName": config_secret
                    }
                },
                {
                    "name": "data",
                    "persistentVolumeClaim": {
                        "claimName": clone_pvc,
                        "readOnly": True
                    }
                },
                {
                    "name": "cache",
                    "persistentVolumeClaim": {
                        "claimName": cache_pvc
                    }
                }
            ]
        }
    }

    return manifest


def stream_pod_logs(
    v1: client.CoreV1Api,
    pod_name: str,
    namespace: str,
    stop_event: threading.Event
) -> None:
    """Stream pod logs to stdout in real-time (runs in background thread).

    Args:
        v1: CoreV1Api client
        pod_name: Pod name to stream logs from
        namespace: Kubernetes namespace
        stop_event: Threading event to signal when to stop streaming
    """
    try:
        # Wait for CONTAINER to be running (not just pod phase!)
        max_wait = 60
        start_time = time.time()

        while time.time() - start_time < max_wait:
            if stop_event.is_set():
                return

            try:
                pod = v1.read_namespaced_pod(pod_name, namespace)

                # Check container status (not just pod phase)
                if pod.status.container_statuses:
                    for container in pod.status.container_statuses:
                        if container.state.running and container.state.running.started_at:
                            # Container running! Logs guaranteed available!
                            # Break out of both loops
                            break
                    else:
                        # Continue if no running container found
                        time.sleep(1)
                        continue
                    # Container found running, break outer loop
                    break
            except ApiException:
                pass

            time.sleep(1)

        # NOW use follow=True - will stream until pod completes
        try:
            log_stream = v1.read_namespaced_pod_log(
                pod_name,
                namespace,
                follow=True,  # Safe now - container is running
                _preload_content=False
            )

            for line in log_stream:
                if stop_event.is_set():
                    break
                line_str = line.decode('utf-8').rstrip('\n\r')
                if line_str:
                    print(f"[{pod_name}] {line_str}", flush=True)

        except ApiException as exc:
            # Only log if actually an error (not normal stop)
            if not stop_event.is_set():
                reason = exc.reason if hasattr(exc, 'reason') else exc
                log_msg(f"⚠️  Log streaming ended for {pod_name}: {reason}")

    except Exception as exc:
        log_msg(f"⚠️  Error streaming logs for {pod_name}: {exc}")


def stream_pod_events(
    v1: client.CoreV1Api,
    pod_name: str,
    namespace: str,
    stop_event: threading.Event
) -> None:
    """Stream pod events to stdout in real-time (runs in background thread).

    This function continuously watches for events until stop_event is set.
    It automatically reconnects when watch timeouts occur to ensure
    continuous event monitoring throughout pod lifecycle.

    Args:
        v1: CoreV1Api client
        pod_name: Pod name to stream events for
        namespace: Kubernetes namespace
        stop_event: Threading event to signal when to stop streaming
    """
    try:
        latest_resource_version = None

        while not stop_event.is_set():
            w = watch.Watch()

            try:
                # Build kwargs with optional resourceVersion
                kwargs = {
                    'namespace': namespace,
                    'field_selector': f"involvedObject.kind=Pod,involvedObject.name={pod_name}",
                    'timeout_seconds': 60
                }

                # Resume from last seen event (no duplicates!)
                if latest_resource_version:
                    kwargs['resource_version'] = latest_resource_version

                for event in w.stream(v1.list_namespaced_event, **kwargs):
                    if stop_event.is_set():
                        break

                    obj = event['object']
                    print(f"[EVENT] {obj.reason}: {obj.message}", flush=True)

                    # Track resourceVersion for next reconnect
                    latest_resource_version = obj.metadata.resource_version

            except ApiException as exc:
                # Ignore status 410 (resource version too old) - normal on reconnect
                if hasattr(exc, 'status') and exc.status == 410:
                    # Reset resourceVersion, will get fresh events
                    latest_resource_version = None
                    time.sleep(1)
                    continue

                if not stop_event.is_set():
                    reason = exc.reason if hasattr(exc, 'reason') else exc
                    log_msg(f"⚠️  Event watch interrupted for {pod_name}: {reason}")
                break

            finally:
                w.stop()

            # Brief pause before reconnect
            if not stop_event.is_set():
                time.sleep(1)

    except Exception as exc:
        log_msg(f"⚠️  Error streaming events for {pod_name}: {exc}")


def spawn_borg_pod(
    v1: client.CoreV1Api,
    manifest: dict[str, Any],
    namespace: str,
    timeout: int
) -> bool:
    """Spawn a borg pod and wait for completion with real-time log streaming.

    Args:
        v1: CoreV1Api client
        manifest: Pod manifest
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        True if pod succeeded, False if failed or timeout
    """
    pod_name = manifest["metadata"]["name"]

    try:
        v1.create_namespaced_pod(namespace, manifest)
        _tracked_resources["borg_pods"].append(pod_name)
    except ApiException as exc:
        log_msg(f"❌ Failed to create borg pod {pod_name}: {exc}")
        return False

    log_msg(f"⏳ Waiting for borg pod {pod_name} to complete (timeout: {timeout}s)...")

    # Start event streaming in background thread
    stop_event = threading.Event()
    event_thread = threading.Thread(
        target=stream_pod_events,
        args=(v1, pod_name, namespace, stop_event),
        daemon=True
    )
    event_thread.start()

    # Start log streaming in background thread
    log_thread = threading.Thread(
        target=stream_pod_logs,
        args=(v1, pod_name, namespace, stop_event),
        daemon=True
    )
    log_thread.start()

    # Monitor pod status
    end = time.time() + timeout
    while time.time() < end:
        try:
            pod = v1.read_namespaced_pod(pod_name, namespace)
            phase = pod.status.phase

            if phase in {"Succeeded", "Failed"}:
                # Stop both streaming threads
                stop_event.set()
                log_thread.join(timeout=5)  # Wait up to 5s for log thread to finish
                event_thread.join(timeout=5)  # Wait up to 5s for event thread to finish

                if phase == "Succeeded":
                    log_msg(f"✅ Borg pod {pod_name} completed successfully")
                    return True
                else:
                    log_msg(f"❌ Borg pod {pod_name} failed")
                    return False

        except ApiException as exc:
            log_msg(f"⚠️  Error reading pod {pod_name}: {exc}")
            stop_event.set()
            return False

        time.sleep(10)

    # Timeout reached
    stop_event.set()
    log_msg(f"❌ Borg pod {pod_name} timeout after {timeout}s")
    return False


def delete_pod(v1: client.CoreV1Api, name: str, namespace: str) -> None:
    """Delete a pod and remove from tracking."""
    try:
        v1.delete_namespaced_pod(name, namespace)
        if name in _tracked_resources["borg_pods"]:
            _tracked_resources["borg_pods"].remove(name)
    except ApiException:
        pass


def delete_pvc(v1: client.CoreV1Api, name: str, namespace: str) -> None:
    """Delete a PVC and remove from tracking."""
    try:
        v1.delete_namespaced_persistent_volume_claim(name, namespace)
        if name in _tracked_resources["clone_pvcs"]:
            _tracked_resources["clone_pvcs"].remove(name)
    except ApiException:
        pass


def delete_secret(v1: client.CoreV1Api, name: str, namespace: str) -> None:
    """Delete a secret and remove from tracking."""
    try:
        v1.delete_namespaced_secret(name, namespace)
        if name in _tracked_resources["ssh_secrets"]:
            _tracked_resources["ssh_secrets"].remove(name)
    except ApiException:
        pass


def create_single_clone_pvc(
    v1: client.CoreV1Api,
    snap_api: client.CustomObjectsApi,
    storage_api: client.StorageV1Api,
    backup_config: dict[str, Any],
    namespace: str
) -> ClonePVC:
    """Create a single clone PVC from snapshot.

    This function handles all steps for creating one clone PVC:
    - Validate backup config
    - Find latest snapshot
    - Validate storage class exists
    - Create clone PVC
    - Track it for cleanup

    Args:
        v1: CoreV1Api client
        snap_api: CustomObjectsApi client
        storage_api: StorageV1Api client
        backup_config: Backup configuration
        namespace: Kubernetes namespace

    Returns:
        ClonePVC object (with failed=True if creation failed)
    """
    name = backup_config.get("name", "unknown")
    pvc = backup_config.get("pvc")
    storage_class = backup_config.get("class")

    # Validate required fields
    if not all([name, pvc, storage_class]):
        log_msg(f"❌ Backup '{name}': Missing required fields (name, pvc, class)")
        return ClonePVC(
            backup_name=name,
            pvc_name=pvc or "",
            clone_name="",
            snapshot_name="",
            backup_config=backup_config,
            failed=True,
            failure_reason="Config error - missing required fields"
        )

    # Type narrowing
    assert isinstance(name, str)
    assert isinstance(pvc, str)
    assert isinstance(storage_class, str)

    try:
        # Find latest snapshot
        log_msg(f"🔍 [{name}] Finding latest snapshot for PVC: {pvc}")
        snap_name = latest_snapshot(snap_api, pvc, namespace)
        if not snap_name:
            log_msg(f"❌ [{name}] No ready snapshot found for PVC: {pvc}")
            return ClonePVC(
                backup_name=name,
                pvc_name=pvc,
                clone_name="",
                snapshot_name="",
                backup_config=backup_config,
                failed=True,
                failure_reason="No snapshot found"
            )
        log_msg(f"✅ [{name}] Found snapshot: {snap_name}")

        # Validate storage class exists
        log_msg(f"🔍 [{name}] Validating storage class: {storage_class}")
        exists, error_msg = validate_storage_class(storage_api, storage_class)
        if not exists:
            log_msg(f"❌ [{name}] {error_msg}")
            return ClonePVC(
                backup_name=name,
                pvc_name=pvc,
                clone_name="",
                snapshot_name=snap_name,
                backup_config=backup_config,
                failed=True,
                failure_reason=error_msg
            )
        log_msg(f"✅ [{name}] Storage class validated")

        # Create clone PVC
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        clone_name = f"{snap_name}-clone-{ts}"
        log_msg(f"📦 [{name}] Creating clone PVC: {clone_name}")
        create_clone_pvc(v1, snap_api, snap_name, clone_name, storage_class, namespace)
        log_msg(f"✅ [{name}] Clone PVC created")

        return ClonePVC(
            backup_name=name,
            pvc_name=pvc,
            clone_name=clone_name,
            snapshot_name=snap_name,
            backup_config=backup_config,
            failed=False
        )

    except Exception as exc:
        log_msg(f"❌ [{name}] Unexpected error creating clone PVC: {exc}")
        return ClonePVC(
            backup_name=name,
            pvc_name=pvc,
            clone_name="",
            snapshot_name="",
            backup_config=backup_config,
            failed=True,
            failure_reason=str(exc)
        )


def create_all_clone_pvcs(
    v1: client.CoreV1Api,
    snap_api: client.CustomObjectsApi,
    storage_api: client.StorageV1Api,
    backups: list[dict[str, Any]],
    namespace: str
) -> list[ClonePVC]:
    """Create all clone PVCs in parallel (non-blocking).

    This function starts all clone PVC creation requests in parallel but
    does NOT wait for them to be ready. Each clone will be checked
    individually in Phase 2 right before its backup runs.

    Args:
        v1: CoreV1Api client
        snap_api: CustomObjectsApi client
        storage_api: StorageV1Api client
        backups: List of backup configurations
        namespace: Kubernetes namespace

    Returns:
        List of ClonePVC objects (ready state unknown at return time)
    """
    log_msg(f"\n{'='*60}")
    log_msg("📦 Phase 1: Creating ALL clone PVCs in parallel")
    log_msg(f"{'='*60}")

    clone_pvcs: list[ClonePVC] = []

    # Create all clone PVCs in parallel using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=len(backups)) as executor:
        futures = {
            executor.submit(
                create_single_clone_pvc,
                v1, snap_api, storage_api, backup_cfg, namespace
            ): backup_cfg
            for backup_cfg in backups
        }

        for future in futures:
            clone_pvc = future.result()
            clone_pvcs.append(clone_pvc)

            if clone_pvc.failed:
                log_msg(f"❌ [{clone_pvc.backup_name}] Clone creation failed: {clone_pvc.failure_reason}")
                _failures.append(f"{clone_pvc.backup_name}: {clone_pvc.failure_reason}")

    log_msg("✅ All clone PVC creation requests submitted in parallel")
    log_msg("📝 Note: Clone PVCs will be checked individually before each backup")

    return clone_pvcs


def process_backup_with_clone(
    clone_pvc: ClonePVC,
    v1: client.CoreV1Api,
    release_name: str,
    pod_config: dict[str, Any],
    borg_repo: str,
    borg_passphrase: str,
    ssh_private_key: str,
    cache_pvc: str,
    retention: dict[str, int],
    namespace: str,
    test_mode: bool
) -> bool:
    """Process a single backup: wait for clone ready, spawn borg pod, cleanup.

    This function waits for the specific clone PVC to be ready (allowing
    other clones to provision in parallel), then runs the borg backup.

    Args:
        clone_pvc: ClonePVC object with clone details
        v1: CoreV1Api client
        release_name: Helm release fullname for pod naming
        pod_config: Pod configuration
        borg_repo: Borg repository URL
        borg_passphrase: Borg passphrase
        ssh_private_key: SSH private key content
        cache_pvc: Borg cache PVC name
        retention: Retention policy
        namespace: Kubernetes namespace
        test_mode: If True, skip borg pod spawn

    Returns:
        True if successful, False if failed
    """
    name = clone_pvc.backup_name
    timeout = clone_pvc.backup_config.get("timeout")

    if not timeout:
        log_msg(f"❌ [{name}] Backup config missing timeout field")
        _failures.append(f"{name}: Config error - missing timeout")
        return False

    assert isinstance(timeout, int)

    log_msg(f"\n{'='*60}")
    log_msg(f"🔄 Processing backup: {name}")
    log_msg(f"{'='*60}")

    # Skip if clone failed in Phase 1
    if clone_pvc.failed:
        log_msg(f"⏭️  [{name}] Skipping - clone PVC creation failed in Phase 1")
        return False

    # Wait for THIS clone PVC to be ready (while other clones provision in background)
    clone_bind_timeout = clone_pvc.backup_config.get("cloneBindTimeout", 300)
    assert isinstance(clone_bind_timeout, int)

    log_msg(f"⏳ [{name}] Waiting for clone PVC to be ready: {clone_pvc.clone_name} (timeout: {clone_bind_timeout}s)")
    success, error_msg = wait_clone_pvc_ready(v1, clone_pvc.clone_name, namespace, clone_bind_timeout)

    if not success:
        log_msg(f"❌ [{name}] Clone PVC not ready: {error_msg}")
        _failures.append(f"{name}: Clone PVC bind failed: {error_msg}")
        return False

    log_msg(f"✅ [{name}] Clone PVC ready - starting backup")

    pod_name = None
    config_secret_name = None

    try:
        # Step 1: Spawn borg pod (or skip in test mode)
        if test_mode:
            log_msg(f"🧪 TEST MODE: Skipping borg pod spawn for {name}")
            log_msg("🧪 TEST MODE: Simulating 2 second backup...")
            time.sleep(2)
            log_msg("✅ TEST MODE: Backup simulation successful")
            return True

        # Step 1a: Create ephemeral secret with config file
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        pod_name = f"{release_name}-borg-{name}-{ts}"
        config_secret_name = f"{pod_name}-config"
        log_msg(f"🔐 Creating ephemeral config secret: {config_secret_name}")
        create_borg_secret(
            v1, config_secret_name,
            borg_repo, borg_passphrase, ssh_private_key,
            retention, name, "/data", timeout,
            namespace
        )
        log_msg("✅ Config secret created")

        # Step 1b: Build and spawn borg pod
        log_msg(f"🚀 Spawning borg pod: {pod_name}")
        manifest = build_borg_pod_manifest(
            pod_name, name, clone_pvc.clone_name, pod_config,
            config_secret_name, cache_pvc,
            timeout, namespace
        )

        if not spawn_borg_pod(v1, manifest, namespace, timeout):
            log_msg(f"❌ Borg backup failed for {name}")
            _failures.append(f"{name}: Borg pod failed")
            return False

        log_msg(f"✅ Backup completed for {name}")
        return True

    except Exception as exc:
        log_msg(f"❌ Unexpected error during backup {name}: {exc}")
        _failures.append(f"{name}: {exc}")
        return False

    finally:
        # Always cleanup
        if config_secret_name:
            log_msg(f"🗑️  Cleaning up config secret: {config_secret_name}")
            delete_secret(v1, config_secret_name, namespace)
        if pod_name:
            log_msg(f"🗑️  Cleaning up borg pod: {pod_name}")
            delete_pod(v1, pod_name, namespace)
        if clone_pvc.clone_name:
            log_msg(f"🗑️  Cleaning up clone PVC: {clone_pvc.clone_name}")
            delete_pvc(v1, clone_pvc.clone_name, namespace)


def main() -> None:
    """Main execution flow with optimized two-phase approach.

    Phase 1: Start ALL clone PVC creation in parallel (non-blocking)
    Phase 2: Process backups SEQUENTIALLY (borg lock limitation)
             - Wait for each specific clone to be ready
             - Run backup while other clones provision in background
             - Cleanup and move to next backup

    This maximizes parallelism - first backup starts as soon as first
    clone is ready, even if other clones are still provisioning.
    """
    global _namespace, _core_api, _storage_api

    # Register SIGTERM handler
    signal.signal(signal.SIGTERM, lambda s, f: cleanup_all_resources())

    args = parse_args()
    cfg = load_config(args.config)

    namespace = cfg.get("namespace")
    if not namespace:
        log_msg("❌ Config missing required field: namespace")
        sys.exit(2)
    _namespace = namespace

    test_mode = args.test

    v1, snap_api, storage_api = init_clients()
    _core_api = v1
    _storage_api = storage_api

    log_msg(f"🔧 Using namespace: {namespace}")
    if test_mode:
        log_msg("🧪 TEST MODE: Borg pods will NOT be spawned")

    # Extract configuration
    release_name = cfg.get("releaseName", "kube-borg-backup")
    backups = cfg.get("backups", [])
    pod_config = cfg.get("pod", {})
    borg_repo = cfg.get("borgRepo")
    borg_passphrase = cfg.get("borgPassphrase")
    ssh_private_key = cfg.get("sshPrivateKey")
    cache_pvc = cfg.get("cachePVC", "borg-cache")
    retention = cfg.get("retention", {})

    if not all([borg_repo, borg_passphrase, ssh_private_key]):
        log_msg("❌ Config missing required fields: borgRepo, borgPassphrase, sshPrivateKey")
        sys.exit(2)

    # Type narrowing: all fields validated as non-None above
    assert isinstance(borg_repo, str)
    assert isinstance(borg_passphrase, str)
    assert isinstance(ssh_private_key, str)

    if not backups:
        log_msg("⚠️  No backups configured")
        sys.exit(0)

    log_msg(f"\n{'='*60}")
    log_msg(f"🎯 Starting backup process for {len(backups)} backup(s)")
    log_msg(f"{'='*60}")
    log_msg(f"📋 Release: {release_name}")
    log_msg(f"📋 Retention: {retention}")
    log_msg("📋 Strategy: Start all clones in parallel → Wait individually per backup")

    # Phase 1: Create ALL clone PVCs in parallel
    clone_pvcs = create_all_clone_pvcs(v1, snap_api, storage_api, backups, namespace)

    # Phase 2: Process backups SEQUENTIALLY (borg repo only supports one writer)
    log_msg(f"\n{'='*60}")
    log_msg("🔄 Phase 2: Processing backups SEQUENTIALLY")
    log_msg(f"{'='*60}")

    for clone_pvc in clone_pvcs:
        _ = process_backup_with_clone(  # Result unused, failures tracked in _failures global
            clone_pvc, v1, release_name, pod_config,
            borg_repo, borg_passphrase, ssh_private_key, cache_pvc,
            retention, namespace, test_mode
        )
        # Continue even on failure (report all failures at end)

    # Report results
    log_msg(f"\n{'='*60}")
    log_msg("📊 Backup Process Complete")
    log_msg(f"{'='*60}")

    if _failures:
        log_msg(f"\n❌ {len(_failures)} backup(s) failed:")
        for failure in _failures:
            log_msg(f"  - {failure}")
        log_msg("\n❌ Backup process completed with errors")
        sys.exit(1)

    log_msg("\n✅ All backups completed successfully!")


if __name__ == "__main__":
    main()
