"""Create and prune PVC snapshots using the Kubernetes API.

The script expects a YAML config mounted at /config/config.yaml with the
following structure::

    snapshots:
      schedule: "0 */4 * * *"
      retention:
        hourly: 24
        daily: 7
        weekly: 4
        monthly: 3
      pvcs:
        - name: postgres-data
          snapshotClass: longhorn
          hooks:
            pre:
              - pod: postgres-0
                container: postgres  # optional
                command: ["psql", "-c", "SELECT pg_backup_start()"]
            post:
              - pod: postgres-0
                command: ["psql", "-c", "SELECT pg_backup_stop()"]

It executes pre-hooks sequentially, creates snapshots in parallel, then runs
post-hooks. Post-hooks ALWAYS run (even on failure) and also run on SIGTERM.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import concurrent.futures
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException
from kubernetes.stream import stream

GROUP = "snapshot.storage.k8s.io"
VERSION = "v1"
PLURAL = "volumesnapshots"

# Global state for signal handler
_config: Optional[Dict[str, Any]] = None
_namespace: Optional[str] = None
_core_api: Optional[client.CoreV1Api] = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="PVC snapshot helper")
    parser.add_argument("-c", "--config", help="Path to config file")
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
        print(f"❌ Config file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"❌ Failed to read config {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("❌ Config root must be a mapping", file=sys.stderr)
        sys.exit(2)
    return data


def get_namespace() -> str:
    """Get current namespace from env, service account, or default."""
    # Try environment variable first (for testing)
    env_namespace = os.getenv("NAMESPACE")
    if env_namespace:
        return env_namespace
    # Try to read from service account (in-cluster)
    sa_namespace_path = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if sa_namespace_path.exists():
        return sa_namespace_path.read_text().strip()
    # Fallback to default
    return "default"


def init_clients() -> tuple[client.CustomObjectsApi, client.CoreV1Api]:
    """Initialize Kubernetes API clients."""
    try:
        k8s_config.load_incluster_config()
    except ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:
            print(f"❌ Failed to load kubeconfig: {exc}", file=sys.stderr)
            sys.exit(3)
    return client.CustomObjectsApi(), client.CoreV1Api()


def run_pod_exec_hook(
    core_api: client.CoreV1Api,
    hook: Dict[str, Any],
    namespace: str
) -> None:
    """Execute a command in a pod via Kubernetes API.

    Args:
        core_api: CoreV1Api client
        hook: Hook config with pod, optional container, and command
        namespace: Kubernetes namespace

    Raises:
        ApiException: If pod exec fails
    """
    pod_name = hook.get("pod")
    container = hook.get("container")  # Optional
    command = hook.get("command", [])

    if not pod_name or not command:
        print(f"⚠️  Hook missing pod or command: {hook}", file=sys.stderr)
        return

    print(f"🔧 Executing in pod {pod_name}: {' '.join(command)}")

    try:
        resp = stream(
            core_api.connect_get_namespaced_pod_exec,
            pod_name,
            namespace,
            command=command,
            container=container,
            stderr=True,
            stdin=False,
            stdout=True,
            tty=False,
            _preload_content=False
        )

        # Read output
        resp.run_forever(timeout=60)
        if resp.returncode != 0:
            print(f"❌ Hook failed with exit code {resp.returncode}", file=sys.stderr)
            raise RuntimeError(f"Hook command failed: {resp.returncode}")

        print(f"✅ Hook completed successfully")

    except ApiException as exc:
        print(f"❌ Failed to exec in pod {pod_name}: {exc}", file=sys.stderr)
        raise


def run_hooks(
    core_api: client.CoreV1Api,
    hooks: List[Dict[str, Any]],
    namespace: str,
    hook_type: str
) -> None:
    """Run a list of hooks sequentially.

    Args:
        core_api: CoreV1Api client
        hooks: List of hook configurations
        namespace: Kubernetes namespace
        hook_type: "pre" or "post" for logging

    Raises:
        RuntimeError: If any hook fails (only for pre-hooks)
    """
    if not hooks:
        return

    print(f"\n{'='*60}")
    print(f"🔄 Running {hook_type}-hooks ({len(hooks)} total)")
    print(f"{'='*60}\n")

    for i, hook in enumerate(hooks, 1):
        print(f"[{i}/{len(hooks)}] {hook_type.capitalize()}-hook:")
        try:
            run_pod_exec_hook(core_api, hook, namespace)
        except Exception as exc:
            if hook_type == "pre":
                # Pre-hooks must succeed
                print(f"\n❌ Pre-hook {i} failed, aborting!", file=sys.stderr)
                raise
            else:
                # Post-hooks: log error but continue
                print(f"\n⚠️  Post-hook {i} failed: {exc}", file=sys.stderr)
                print("Continuing with remaining post-hooks...")


def create_snapshot(
    api: client.CustomObjectsApi,
    pvc_name: str,
    snapshot_class: str,
    namespace: str
) -> str:
    """Create a VolumeSnapshot for a PVC.

    Returns:
        Snapshot name
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    snap_name = f"{pvc_name}-snap-{ts}"

    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "VolumeSnapshot",
        "metadata": {
            "name": snap_name,
            "namespace": namespace,
            "labels": {"pvc": pvc_name, "managed-by": "kube-borg-backup"}
        },
        "spec": {
            "volumeSnapshotClassName": snapshot_class,
            "source": {"persistentVolumeClaimName": pvc_name},
        },
    }

    api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
    return snap_name


def wait_snapshot_ready(
    api: client.CustomObjectsApi,
    name: str,
    namespace: str,
    timeout: int = 60
) -> None:
    """Wait for snapshot to become ready.

    Args:
        api: CustomObjectsApi client
        name: Snapshot name
        namespace: Kubernetes namespace
        timeout: Timeout in seconds

    Raises:
        TimeoutError: If snapshot not ready within timeout
    """
    end = time.time() + timeout
    while time.time() < end:
        snap = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        if snap.get("status", {}).get("readyToUse"):
            return
        time.sleep(2)
    raise TimeoutError(f"Snapshot {name} not ready after {timeout}s")


def create_snapshot_for_pvc(
    api: client.CustomObjectsApi,
    pvc_config: Dict[str, Any],
    namespace: str
) -> str:
    """Create and wait for snapshot for a single PVC."""
    pvc_name = pvc_config.get("name")
    snapshot_class = pvc_config.get("snapshotClass")

    if not pvc_name or not snapshot_class:
        raise ValueError(f"PVC config missing name or snapshotClass: {pvc_config}")

    print(f"📸 Creating snapshot for PVC: {pvc_name}")
    snap_name = create_snapshot(api, pvc_name, snapshot_class, namespace)
    print(f"⏳ Waiting for snapshot {snap_name} to become ready...")
    wait_snapshot_ready(api, snap_name, namespace)
    print(f"✅ Snapshot {snap_name} ready!")
    return snap_name


def prune_snapshots_tiered(
    api: client.CustomObjectsApi,
    pvc_name: str,
    retention: Dict[str, int],
    namespace: str
) -> None:
    """Prune snapshots using tiered retention policy.

    Keeps snapshots according to hourly/daily/weekly/monthly buckets.

    Args:
        api: CustomObjectsApi client
        pvc_name: PVC name to prune snapshots for
        retention: Dict with hourly, daily, weekly, monthly counts
        namespace: Kubernetes namespace
    """
    # Fetch all snapshots for this PVC
    snaps = api.list_namespaced_custom_object(
        GROUP, VERSION, namespace, PLURAL,
        label_selector=f"pvc={pvc_name}"
    )
    items = snaps.get("items", [])

    if not items:
        return

    # Sort by creation time (newest first)
    items.sort(
        key=lambda s: s.get("metadata", {}).get("creationTimestamp", ""),
        reverse=True
    )

    now = datetime.now(timezone.utc)
    preserve_set = set()

    # Hourly: Keep 1 per hour for last N hours
    hourly_keep = retention.get("hourly", 0)
    if hourly_keep > 0:
        hourly_buckets: Dict[str, str] = {}  # hour -> snapshot name
        for snap in items:
            ts_str = snap.get("metadata", {}).get("creationTimestamp")
            if not ts_str:
                continue
            try:
                created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if (now - created).total_seconds() > hourly_keep * 3600:
                    continue
                hour_key = created.strftime("%Y-%m-%d-%H")
                if hour_key not in hourly_buckets:
                    hourly_buckets[hour_key] = snap["metadata"]["name"]
            except (ValueError, KeyError):
                continue
        preserve_set.update(hourly_buckets.values())

    # Daily: Keep 1 per day for last N days
    daily_keep = retention.get("daily", 0)
    if daily_keep > 0:
        daily_buckets: Dict[str, str] = {}
        for snap in items:
            ts_str = snap.get("metadata", {}).get("creationTimestamp")
            if not ts_str:
                continue
            try:
                created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if (now - created).days > daily_keep:
                    continue
                day_key = created.strftime("%Y-%m-%d")
                if day_key not in daily_buckets:
                    daily_buckets[day_key] = snap["metadata"]["name"]
            except (ValueError, KeyError):
                continue
        preserve_set.update(daily_buckets.values())

    # Weekly: Keep 1 per week for last N weeks
    weekly_keep = retention.get("weekly", 0)
    if weekly_keep > 0:
        weekly_buckets: Dict[str, str] = {}
        for snap in items:
            ts_str = snap.get("metadata", {}).get("creationTimestamp")
            if not ts_str:
                continue
            try:
                created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if (now - created).days > weekly_keep * 7:
                    continue
                # ISO week: year-week
                week_key = created.strftime("%Y-W%W")
                if week_key not in weekly_buckets:
                    weekly_buckets[week_key] = snap["metadata"]["name"]
            except (ValueError, KeyError):
                continue
        preserve_set.update(weekly_buckets.values())

    # Monthly: Keep 1 per month for last N months
    monthly_keep = retention.get("monthly", 0)
    if monthly_keep > 0:
        monthly_buckets: Dict[str, str] = {}
        for snap in items:
            ts_str = snap.get("metadata", {}).get("creationTimestamp")
            if not ts_str:
                continue
            try:
                created = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                # Approximate months (30 days)
                if (now - created).days > monthly_keep * 30:
                    continue
                month_key = created.strftime("%Y-%m")
                if month_key not in monthly_buckets:
                    monthly_buckets[month_key] = snap["metadata"]["name"]
            except (ValueError, KeyError):
                continue
        preserve_set.update(monthly_buckets.values())

    # Delete snapshots not in preserve set
    deleted_count = 0
    for snap in items:
        snap_name = snap.get("metadata", {}).get("name")
        if snap_name in preserve_set:
            continue
        try:
            api.delete_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, snap_name)
            print(f"🗑️  Deleted old snapshot: {snap_name}")
            deleted_count += 1
        except ApiException as exc:
            print(f"⚠️  Failed to delete snapshot {snap_name}: {exc}", file=sys.stderr)

    if deleted_count > 0:
        print(f"✅ Pruned {deleted_count} old snapshot(s) for PVC {pvc_name}")


def cleanup_post_hooks():
    """Signal handler: Always run post-hooks on termination."""
    if _config and _core_api and _namespace:
        print("\n\n🛑 Received SIGTERM - running post-hooks before exit...")
        pvcs = _config.get("snapshots", {}).get("pvcs", [])
        all_post_hooks = []
        for pvc_cfg in pvcs:
            post_hooks = pvc_cfg.get("hooks", {}).get("post", [])
            all_post_hooks.extend(post_hooks)

        if all_post_hooks:
            try:
                run_hooks(_core_api, all_post_hooks, _namespace, "post")
            except Exception as exc:
                print(f"❌ Post-hooks failed during cleanup: {exc}", file=sys.stderr)
    sys.exit(0)


def main() -> None:
    """Main execution flow."""
    global _config, _namespace, _core_api

    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGTERM, lambda s, f: cleanup_post_hooks())

    args = parse_args()
    cfg = load_config(args.config)
    _config = cfg

    namespace = get_namespace()
    _namespace = namespace
    print(f"🔧 Using namespace: {namespace}\n")

    custom_api, core_api = init_clients()
    _core_api = core_api

    snapshot_config = cfg.get("snapshots", {})
    pvcs = snapshot_config.get("pvcs", [])
    retention = snapshot_config.get("retention", {})

    if not pvcs:
        print("⚠️  No PVCs configured for snapshot", file=sys.stderr)
        sys.exit(0)

    # Collect all pre-hooks from all PVCs
    all_pre_hooks = []
    for pvc_cfg in pvcs:
        pre_hooks = pvc_cfg.get("hooks", {}).get("pre", [])
        all_pre_hooks.extend(pre_hooks)

    # Collect all post-hooks from all PVCs
    all_post_hooks = []
    for pvc_cfg in pvcs:
        post_hooks = pvc_cfg.get("hooks", {}).get("post", [])
        all_post_hooks.extend(post_hooks)

    snapshot_failed = False

    try:
        # Step 1: Run all pre-hooks sequentially (fail-fast)
        if all_pre_hooks:
            run_hooks(core_api, all_pre_hooks, namespace, "pre")

        # Step 2: Create snapshots in parallel
        print(f"\n{'='*60}")
        print(f"📸 Creating {len(pvcs)} snapshot(s) in parallel")
        print(f"{'='*60}\n")

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(pvcs)) as executor:
            futures = {
                executor.submit(create_snapshot_for_pvc, custom_api, pvc_cfg, namespace): pvc_cfg
                for pvc_cfg in pvcs
            }

            for future in concurrent.futures.as_completed(futures):
                pvc_cfg = futures[future]
                try:
                    snap_name = future.result()
                except Exception as exc:
                    pvc_name = pvc_cfg.get("name", "unknown")
                    print(f"❌ Failed to create snapshot for {pvc_name}: {exc}", file=sys.stderr)
                    snapshot_failed = True

        # Step 3: Prune old snapshots
        if retention:
            print(f"\n{'='*60}")
            print(f"🗑️  Pruning old snapshots")
            print(f"{'='*60}\n")

            for pvc_cfg in pvcs:
                pvc_name = pvc_cfg.get("name")
                if pvc_name:
                    prune_snapshots_tiered(custom_api, pvc_name, retention, namespace)

    except Exception as exc:
        print(f"\n❌ Error during snapshot process: {exc}", file=sys.stderr)
        snapshot_failed = True

    finally:
        # Step 4: ALWAYS run post-hooks (even on failure)
        if all_post_hooks:
            try:
                run_hooks(core_api, all_post_hooks, namespace, "post")
            except Exception as exc:
                print(f"❌ Post-hooks failed: {exc}", file=sys.stderr)
                snapshot_failed = True

    if snapshot_failed:
        print("\n❌ Snapshot process completed with errors", file=sys.stderr)
        sys.exit(1)

    print("\n✅ Snapshot process completed successfully!")


if __name__ == "__main__":
    main()
