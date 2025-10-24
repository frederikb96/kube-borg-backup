"""Restore snapshots to temporary PVCs and run borg backups.

This module orchestrates the BorgBackup process:
1. Create clone PVCs from VolumeSnapshots (parallel for speed)
2. Execute backups sequentially (borg repo only supports one writer)
3. Clean up temporary resources (always, even on SIGTERM)

Each backup creates a temporary clone PVC from the latest snapshot, spawns
a borg pod to back it up to the remote repository, then deletes both
resources. The process continues even if individual backups fail, reporting
all failures at the end.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

SNAP_GROUP = "snapshot.storage.k8s.io"
SNAP_VERSION = "v1"
SNAP_PLURAL = "volumesnapshots"

# Global state for SIGTERM handler
_tracked_resources: dict[str, list[str]] = {"clone_pvcs": [], "borg_pods": [], "ssh_secrets": []}
_namespace: str | None = None
_core_api: client.CoreV1Api | None = None
_failures: list[str] = []


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


def init_clients() -> tuple[client.CoreV1Api, client.CustomObjectsApi]:
    """Initialize Kubernetes API clients."""
    try:
        k8s_config.load_incluster_config()
    except ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:
            log_msg(f"❌ Failed to load kubeconfig: {exc}")
            sys.exit(3)
    return client.CoreV1Api(), client.CustomObjectsApi()


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
) -> bool:
    """Wait for clone PVC to be Bound or WaitForFirstConsumer.

    Handles both Immediate and WaitForFirstConsumer storage classes.
    For WaitForFirstConsumer, the PVC won't bind until a pod uses it.

    Args:
        v1: CoreV1Api client
        pvc_name: Name of PVC to wait for
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        True if PVC is ready, False on timeout
    """
    start_time = time.time()

    while True:
        elapsed = int(time.time() - start_time)

        # Check timeout
        if elapsed >= timeout:
            log_msg(f"⏰ Timeout waiting for PVC {pvc_name} after {elapsed}s")
            return False

        try:
            # Get PVC status
            pvc = v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
            status = pvc.status.phase

            # Check if Bound
            if status == "Bound":
                log_msg(f"✅ PVC {pvc_name} is Bound after {elapsed}s")
                return True

            # Check if WaitForFirstConsumer (ready to be used by pod)
            if status == "Pending":
                # Get events for the PVC
                events = v1.list_namespaced_event(
                    namespace,
                    field_selector=f"involvedObject.name={pvc_name},involvedObject.kind=PersistentVolumeClaim"
                )
                for event in events.items:
                    if "WaitForFirstConsumer" in event.message or "waiting for first consumer" in event.message:
                        log_msg(f"🕓 PVC {pvc_name} waiting for first consumer after {elapsed}s - ready to use")
                        return True

        except ApiException as exc:
            log_msg(f"⚠️ Error checking PVC {pvc_name}: {exc}")
            return False

        time.sleep(5)


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
                        f"{pod_config.get('image', {}).get('repository', 'ghcr.io/frederikb96/kube-borg-backup-essentials')}"  # noqa: E501
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


def spawn_borg_pod(
    v1: client.CoreV1Api,
    manifest: dict[str, Any],
    namespace: str,
    timeout: int
) -> bool:
    """Spawn a borg pod and wait for completion.

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

    end = time.time() + timeout
    while time.time() < end:
        try:
            pod = v1.read_namespaced_pod(pod_name, namespace)
            phase = pod.status.phase

            if phase in {"Succeeded", "Failed"}:
                # Stream logs
                try:
                    logs = v1.read_namespaced_pod_log(pod_name, namespace)
                    if logs:
                        log_msg("\n--- Borg Pod Logs ---")
                        log_msg(logs)
                        log_msg("--- End Logs ---\n")
                except ApiException:
                    log_msg("⚠️  Could not retrieve pod logs")

                if phase == "Succeeded":
                    log_msg(f"✅ Borg pod {pod_name} completed successfully")
                    return True
                else:
                    log_msg(f"❌ Borg pod {pod_name} failed")
                    return False

        except ApiException as exc:
            log_msg(f"⚠️  Error reading pod {pod_name}: {exc}")
            return False

        time.sleep(10)

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


def process_backup(
    backup_config: dict[str, Any],
    v1: client.CoreV1Api,
    snap_api: client.CustomObjectsApi,
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
    """Process a single backup: create clone, spawn borg pod, cleanup.

    Args:
        backup_config: Backup configuration (name, pvc, class, timeout)
        v1: CoreV1Api client
        snap_api: CustomObjectsApi client
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
    name = backup_config.get("name")
    pvc = backup_config.get("pvc")
    storage_class = backup_config.get("class")
    timeout = backup_config.get("timeout")
    clone_bind_timeout = backup_config.get("cloneBindTimeout")

    if not all([name, pvc, storage_class, timeout, clone_bind_timeout]):
        log_msg(
            f"❌ Backup config missing required fields "
            f"(name, pvc, class, timeout, cloneBindTimeout): {backup_config}"
        )
        _failures.append(f"{name or 'unknown'}: Config error - missing required fields")
        return False

    # Type narrowing: all fields validated as non-None above
    assert isinstance(name, str)
    assert isinstance(pvc, str)
    assert isinstance(storage_class, str)
    assert isinstance(timeout, int)
    assert isinstance(clone_bind_timeout, int)

    log_msg(f"\n{'='*60}")
    log_msg(f"🔄 Starting backup: {name}")
    log_msg(f"{'='*60}")

    clone_name = None
    pod_name = None
    config_secret_name = None

    try:
        # Step 1: Find latest snapshot
        log_msg(f"🔍 Finding latest snapshot for PVC: {pvc}")
        snap_name = latest_snapshot(snap_api, pvc, namespace)
        if not snap_name:
            log_msg(f"❌ No ready snapshot found for PVC: {pvc}")
            _failures.append(f"{name}: No snapshot found")
            return False
        log_msg(f"✅ Found snapshot: {snap_name}")

        # Step 2: Create clone PVC
        ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
        clone_name = f"{snap_name}-clone-{ts}"
        log_msg(f"📦 Creating clone PVC: {clone_name}")
        create_clone_pvc(v1, snap_api, snap_name, clone_name, storage_class, namespace)
        log_msg("✅ Clone PVC created")

        # Step 3: Wait for clone PVC to be ready
        log_msg(f"⏳ Waiting for clone PVC to be ready (timeout: {clone_bind_timeout}s)...")
        if not wait_clone_pvc_ready(v1, clone_name, namespace, clone_bind_timeout):
            log_msg(f"❌ Clone PVC {clone_name} not ready after {clone_bind_timeout}s")
            _failures.append(f"{name}: Clone PVC bind timeout")
            return False

        # Step 4: Spawn borg pod (or skip in test mode)
        if test_mode:
            log_msg(f"🧪 TEST MODE: Skipping borg pod spawn for {name}")
            log_msg("🧪 TEST MODE: Simulating 2 second backup...")
            time.sleep(2)
            log_msg("✅ TEST MODE: Backup simulation successful")
            return True

        # Step 4a: Create ephemeral secret with config file
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

        # Step 4b: Build and spawn borg pod
        log_msg(f"🚀 Spawning borg pod: {pod_name}")
        manifest = build_borg_pod_manifest(
            pod_name, name, clone_name, pod_config,
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
        if clone_name:
            log_msg(f"🗑️  Cleaning up clone PVC: {clone_name}")
            delete_pvc(v1, clone_name, namespace)


def main() -> None:
    """Main execution flow."""
    global _namespace, _core_api

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

    v1, snap_api = init_clients()
    _core_api = v1

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
    log_msg(f"🎯 Processing {len(backups)} backup(s) SEQUENTIALLY")
    log_msg(f"{'='*60}")
    log_msg(f"📋 Release: {release_name}")
    log_msg(f"📋 Retention: {retention}")

    # Process backups sequentially (borg repo only supports one writer)
    for backup_cfg in backups:
        _ = process_backup(  # Result unused, failures tracked in _failures global
            backup_cfg, v1, snap_api, release_name, pod_config,
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
