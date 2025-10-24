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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

SNAP_GROUP = "snapshot.storage.k8s.io"
SNAP_VERSION = "v1"
SNAP_PLURAL = "volumesnapshots"

# Global state for SIGTERM handler
_tracked_resources: Dict[str, List[str]] = {"clone_pvcs": [], "borg_pods": []}
_namespace: Optional[str] = None
_core_api: Optional[client.CoreV1Api] = None
_failures: List[str] = []


def log_msg(msg: str) -> None:
    """Log message to stdout with consistent formatting."""
    print(msg)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Run borg backups from PVC snapshots")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument("--test", action="store_true", help="Test mode: skip borg pod spawn")
    return parser.parse_args()


def resolve_config_path(cli_path: Optional[str]) -> Path:
    """Resolve the config file path from CLI, env, or default."""
    if cli_path:
        return Path(cli_path)
    env_path = os.getenv("APP_CONFIG")
    if env_path:
        return Path(env_path)
    return Path("/config/config.yaml")


def load_config(cli_path: Optional[str]) -> Dict[str, Any]:
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
) -> Optional[str]:
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
            "accessModes": ["ReadWriteOnce"],
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


def wait_pvc_bound(
    v1: client.CoreV1Api,
    name: str,
    namespace: str,
    timeout: int = 300
) -> bool:
    """Wait for a PVC to reach Bound state.

    Args:
        v1: CoreV1Api client
        name: PVC name
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Returns:
        True if bound, False if timeout
    """
    end = time.time() + timeout
    while time.time() < end:
        try:
            pvc = v1.read_namespaced_persistent_volume_claim(name, namespace)
            if pvc.status.phase == "Bound":
                return True
        except ApiException as exc:
            log_msg(f"⚠️  Error reading PVC {name}: {exc}")
            return False
        time.sleep(5)
    return False


def build_borg_pod_manifest(
    pod_name: str,
    backup_name: str,
    clone_pvc: str,
    pod_config: Dict[str, Any],
    repo_secret: str,
    ssh_secret: str,
    cache_pvc: str,
    retention: Dict[str, int],
    pvc_timeout: int,
    namespace: str
) -> Dict[str, Any]:
    """Build borg pod manifest as pure Python dict.

    Args:
        pod_name: Name for the borg pod
        backup_name: Backup identifier (archive prefix)
        clone_pvc: Name of clone PVC to mount
        pod_config: Pod configuration (image, resources)
        repo_secret: Name of secret with borg repo credentials
        ssh_secret: Name of secret with SSH keys
        cache_pvc: Name of borg cache PVC
        retention: Retention policy (hourly, daily, weekly, monthly, yearly)
        pvc_timeout: Per-PVC timeout (pod activeDeadlineSeconds and lock-wait)
        namespace: Kubernetes namespace

    Returns:
        Pod manifest as dict
    """
    # Build retention env vars
    env_vars = [
        {"name": "BORG_PREFIX", "value": backup_name},
        {"name": "BACKUP_DIR", "value": "/data"},
        {"name": "BORG_LOCK_WAIT", "value": str(pvc_timeout)},
    ]

    # Add retention flags if specified
    if retention.get("hourly"):
        env_vars.append({"name": "BORG_KEEP_HOURLY", "value": str(retention["hourly"])})
    if retention.get("daily"):
        env_vars.append({"name": "BORG_KEEP_DAILY", "value": str(retention["daily"])})
    if retention.get("weekly"):
        env_vars.append({"name": "BORG_KEEP_WEEKLY", "value": str(retention["weekly"])})
    if retention.get("monthly"):
        env_vars.append({"name": "BORG_KEEP_MONTHLY", "value": str(retention["monthly"])})
    if retention.get("yearly"):
        env_vars.append({"name": "BORG_KEEP_YEARLY", "value": str(retention["yearly"])})

    # Add secret refs
    env_vars.extend([
        {
            "name": "BORG_REPO",
            "valueFrom": {
                "secretKeyRef": {
                    "name": repo_secret,
                    "key": "BORG_REPO"
                }
            }
        },
        {
            "name": "BORG_PASSPHRASE",
            "valueFrom": {
                "secretKeyRef": {
                    "name": repo_secret,
                    "key": "BORG_PASSPHRASE"
                }
            }
        }
    ])

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
                    "image": pod_config.get("image", "ghcr.io/frederikb96/kube-borg-backup-essentials:latest"),
                    "env": env_vars,
                    "volumeMounts": [
                        {
                            "name": "data",
                            "mountPath": "/data",
                            "readOnly": True
                        },
                        {
                            "name": "cache",
                            "mountPath": "/cache"
                        },
                        {
                            "name": "ssh",
                            "mountPath": "/root/.ssh",
                            "readOnly": True
                        }
                    ],
                    "resources": pod_config.get("resources", {})
                }
            ],
            "volumes": [
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
                },
                {
                    "name": "ssh",
                    "secret": {
                        "secretName": ssh_secret,
                        "defaultMode": 0o400
                    }
                }
            ]
        }
    }

    return manifest


def spawn_borg_pod(
    v1: client.CoreV1Api,
    manifest: Dict[str, Any],
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


def process_backup(
    backup_config: Dict[str, Any],
    v1: client.CoreV1Api,
    snap_api: client.CustomObjectsApi,
    release_name: str,
    pod_config: Dict[str, Any],
    repo_secret: str,
    ssh_secret: str,
    cache_pvc: str,
    retention: Dict[str, int],
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
        repo_secret: Borg repo secret name
        ssh_secret: SSH secret name
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
    clone_bind_timeout = backup_config.get("cloneBindTimeout", 300)

    if not (name and pvc and storage_class and timeout):
        log_msg(f"❌ Backup config missing required fields (name, pvc, class, timeout): {backup_config}")
        return False

    log_msg(f"\n{'='*60}")
    log_msg(f"🔄 Starting backup: {name}")
    log_msg(f"{'='*60}")

    clone_name = None
    pod_name = None

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
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        clone_name = f"{snap_name}-clone-{ts}"
        log_msg(f"📦 Creating clone PVC: {clone_name}")
        create_clone_pvc(v1, snap_api, snap_name, clone_name, storage_class, namespace)
        log_msg(f"✅ Clone PVC created")

        # Step 3: Wait for clone to bind
        log_msg(f"⏳ Waiting for clone PVC to bind (timeout: {clone_bind_timeout}s)...")
        if not wait_pvc_bound(v1, clone_name, namespace, clone_bind_timeout):
            log_msg(f"❌ Clone PVC {clone_name} not bound after {clone_bind_timeout}s")
            _failures.append(f"{name}: Clone PVC bind timeout")
            return False
        log_msg(f"✅ Clone PVC bound")

        # Step 4: Spawn borg pod (or skip in test mode)
        if test_mode:
            log_msg(f"🧪 TEST MODE: Skipping borg pod spawn for {name}")
            log_msg(f"🧪 TEST MODE: Simulating 2 second backup...")
            time.sleep(2)
            log_msg(f"✅ TEST MODE: Backup simulation successful")
            return True

        pod_name = f"{release_name}-borg-{name}-{ts}"
        log_msg(f"🚀 Spawning borg pod: {pod_name}")
        manifest = build_borg_pod_manifest(
            pod_name, name, clone_name, pod_config,
            repo_secret, ssh_secret, cache_pvc,
            retention, timeout, namespace
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
    repo_secret = cfg.get("repoSecret", "borg-secrets")
    ssh_secret = cfg.get("sshSecret", "borg-ssh")
    cache_pvc = cfg.get("cachePVC", "borg-cache")
    retention = cfg.get("retention", {})

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
        success = process_backup(
            backup_cfg, v1, snap_api, release_name, pod_config,
            repo_secret, ssh_secret, cache_pvc,
            retention, namespace, test_mode
        )
        # Continue even on failure (report all failures at end)

    # Report results
    log_msg(f"\n{'='*60}")
    log_msg(f"📊 Backup Process Complete")
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
