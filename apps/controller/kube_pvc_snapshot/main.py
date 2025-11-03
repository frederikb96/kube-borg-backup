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
from datetime import datetime, UTC
from pathlib import Path
from typing import Any

import yaml
from kubernetes import client, config as k8s_config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

from common.hooks import execute_hooks

GROUP = "snapshot.storage.k8s.io"
VERSION = "v1"
PLURAL = "volumesnapshots"

# Global state for signal handler
_config: dict[str, Any] | None = None
_namespace: str | None = None
_api_client: client.ApiClient | None = None
_active_sessions: dict[str, Any] = {}  # Track active sessionId background execs


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="PVC snapshot helper")
    parser.add_argument("-c", "--config", help="Path to config file")
    parser.add_argument(
        "--test", action="store_true",
        help="Test mode: 5sec delay before snapshots"
    )
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
        print(f"❌ Config file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"❌ Failed to read config {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("❌ Config root must be a mapping", file=sys.stderr)
        sys.exit(2)
    return data


def init_clients() -> tuple[client.CustomObjectsApi, client.ApiClient]:
    """Initialize Kubernetes API clients."""
    try:
        k8s_config.load_incluster_config()
    except ConfigException:
        try:
            k8s_config.load_kube_config()
        except Exception as exc:
            print(f"❌ Failed to load kubeconfig: {exc}", file=sys.stderr)
            sys.exit(3)
    return client.CustomObjectsApi(), client.ApiClient()


def transform_hooks_to_common_format(hooks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Transform controller hook format to common hooks library format.

    Controller format:
        {'pod': 'postgres-0', 'container': 'postgres', 'command': [...]}

    Common library format:
        {'type': 'exec', 'pod': 'postgres-0', 'container': 'postgres', 'command': [...]}

    Args:
        hooks: List of hooks in controller format

    Returns:
        List of hooks in common library format (with 'type': 'exec' added)
    """
    transformed = []
    for hook in hooks:
        # Add 'type': 'exec' to each hook for common library
        transformed_hook = {'type': 'exec', **hook}
        transformed.append(transformed_hook)
    return transformed


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
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
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
    pvc_config: dict[str, Any],
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
    retention: dict[str, int],
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

    now = datetime.now(UTC)
    preserve_set: set[str] = set()

    # Hourly: Keep 1 per hour for last N hours
    hourly_keep = retention.get("hourly", 0)
    if hourly_keep > 0:
        hourly_buckets: dict[str, str] = {}  # hour -> snapshot name
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
        daily_buckets: dict[str, str] = {}
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
        weekly_buckets: dict[str, str] = {}
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
        monthly_buckets: dict[str, str] = {}
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
    """Signal handler: Always run post-hooks on termination.

    Note: Only non-linked post-hooks are executed here.
    Linked session post-hooks are handled by their background exec sessions,
    which will also be terminated by SIGTERM (same pod).
    """
    if _config and _api_client and _namespace:
        print("\n\n🛑 Received SIGTERM - running post-hooks before exit...")
        pvcs = _config.get("snapshots", {}).get("pvcs", [])

        # Collect only non-linked post-hooks
        non_linked_post_hooks = []
        for pvc_cfg in pvcs:
            post_hooks = pvc_cfg.get("hooks", {}).get("post", [])
            for hook in post_hooks:
                if not hook.get('sessionId'):
                    non_linked_post_hooks.append(hook)

        if non_linked_post_hooks:
            try:
                # Transform hooks to common library format
                transformed_hooks = transform_hooks_to_common_format(non_linked_post_hooks)

                print(f"\n{'='*60}")
                print(f"🔄 Running non-linked post-hooks ({len(transformed_hooks)} total)")
                print(f"{'='*60}\n")

                # Execute with common hooks library
                execute_hooks(_api_client, _namespace, transformed_hooks, mode="post")
            except Exception as exc:
                print(f"❌ Post-hooks failed during cleanup: {exc}", file=sys.stderr)
    sys.exit(0)


def group_hooks_by_session(hooks: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Group hooks by sessionId, separating linked from non-linked hooks.

    Args:
        hooks: List of hooks from config

    Returns:
        Tuple of (session_groups, non_linked_hooks)
        - session_groups: {sessionId: {'pre': hook, 'post': hook}}
        - non_linked_hooks: List of hooks without sessionId
    """
    session_groups: dict[str, dict[str, Any]] = {}
    non_linked: list[dict[str, Any]] = []

    for hook in hooks:
        session_id = hook.get('sessionId')
        if session_id:
            if session_id not in session_groups:
                session_groups[session_id] = {}
            # Will be populated with 'pre' or 'post' key by caller
            session_groups[session_id]['hook'] = hook
        else:
            non_linked.append(hook)

    return session_groups, non_linked


def build_linked_session_command(
    pre_hook: dict[str, Any],
    post_hook: dict[str, Any],
    session_id: str
) -> list[str]:
    """Build combined command for sessionId-linked pre/post hooks with checkpoint files.

    Checkpoint files:
    - /tmp/kbb-pre-done-{id}: Written after pre-hook completes
    - /tmp/kbb-signal-{id}: Written by controller to trigger post-hook
    - /tmp/kbb-post-started-{id}: Written when post-hook starts
    - /tmp/kbb-post-done-{id}: Written when post-hook completes

    Args:
        pre_hook: Pre-hook configuration
        post_hook: Post-hook configuration
        session_id: Session identifier

    Returns:
        Command list for kubectl exec
    """
    # Extract commands - expect format: ["/bin/sh", "-c", "shell_script"]
    # We'll embed the shell_script part directly (not double-wrap with another /bin/sh)
    pre_cmd = pre_hook.get('command', [])
    post_cmd = post_hook.get('command', [])

    # Extract shell script content (element at index 2, or join all if different format)
    if len(pre_cmd) >= 3 and pre_cmd[0] in ['/bin/sh', 'sh'] and pre_cmd[1] == '-c':
        pre_script = pre_cmd[2]
    else:
        pre_script = ' '.join(pre_cmd)

    if len(post_cmd) >= 3 and post_cmd[0] in ['/bin/sh', 'sh'] and post_cmd[1] == '-c':
        post_script = post_cmd[2]
    else:
        post_script = ' '.join(post_cmd)

    # Build shell script with checkpoint files
    pre_done_file = f"/tmp/kbb-pre-done-{session_id}"
    signal_file = f"/tmp/kbb-signal-{session_id}"
    post_started_file = f"/tmp/kbb-post-started-{session_id}"
    post_done_file = f"/tmp/kbb-post-done-{session_id}"

    script = f"""
set -e
echo "🔗 [SessionId: {session_id}] Starting linked session..."
echo "🔄 [SessionId: {session_id}] Executing pre-hook..."
{pre_script}
echo "✅ [SessionId: {session_id}] Pre-hook completed"
touch {pre_done_file} || {{ echo "❌ Failed to write checkpoint file {pre_done_file}"; exit 1; }}
echo "⏳ [SessionId: {session_id}] Waiting for snapshot signal..."
while [ ! -f {signal_file} ]; do sleep 1; done
echo "✅ [SessionId: {session_id}] Signal received, executing post-hook..."
touch {post_started_file} || {{ echo "❌ Failed to write checkpoint file {post_started_file}"; exit 1; }}
{post_script}
echo "✅ [SessionId: {session_id}] Post-hook completed"
touch {post_done_file} || {{ echo "❌ Failed to write checkpoint file {post_done_file}"; exit 1; }}
echo "🧹 [SessionId: {session_id}] Cleaning up checkpoint files..."
rm -f {pre_done_file} {signal_file} {post_started_file} {post_done_file}
echo "✅ [SessionId: {session_id}] Session completed successfully"
"""

    return ['/bin/sh', '-c', script.strip()]


def check_file_exists(
    api_client: client.ApiClient,
    namespace: str,
    pod_name: str,
    container: str | None,
    file_path: str
) -> bool:
    """Check if file exists in pod via kubectl exec.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace
        pod_name: Pod name
        container: Optional container name
        file_path: File path to check

    Returns:
        True if file exists, False otherwise
    """
    from kubernetes.stream import stream

    command = ['/bin/sh', '-c', f'[ -f {file_path} ] && echo "EXISTS" || echo "NOT_FOUND"']

    v1 = client.CoreV1Api(api_client)

    exec_kwargs: dict[str, Any] = {
        'name': pod_name,
        'namespace': namespace,
        'command': command,
        'stderr': True,
        'stdout': True,
        'stdin': False,
        'tty': False,
        '_preload_content': False
    }

    if container:
        exec_kwargs['container'] = container

    try:
        resp = stream(v1.connect_get_namespaced_pod_exec, **exec_kwargs)

        # Read output
        stdout_output = ''
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_output += resp.read_stdout()

        return 'EXISTS' in stdout_output

    except Exception:
        return False


def poll_for_checkpoint_file(
    api_client: client.ApiClient,
    namespace: str,
    pod_name: str,
    container: str | None,
    file_path: str,
    session_id: str
) -> None:
    """Poll for checkpoint file until it exists (no timeout, CronJob will kill if needed).

    Args:
        api_client: Kubernetes API client
        namespace: Namespace
        pod_name: Pod name
        container: Optional container name
        file_path: Checkpoint file path to poll for
        session_id: Session identifier for logging

    Raises:
        Exception: If file never appears (CronJob timeout will kill process)
    """
    print(f"⏳ [SessionId: {session_id}] Polling for checkpoint file: {file_path}")

    poll_count = 0
    while True:
        if check_file_exists(api_client, namespace, pod_name, container, file_path):
            print(f"✅ [SessionId: {session_id}] Checkpoint file found after {poll_count} seconds")
            return

        time.sleep(1)
        poll_count += 1

        # Log progress every 10 seconds
        if poll_count % 10 == 0:
            print(f"⏳ [SessionId: {session_id}] Still waiting for {file_path}... ({poll_count}s elapsed)")


def start_linked_session(
    api_client: client.ApiClient,
    namespace: str,
    pre_hook: dict[str, Any],
    post_hook: dict[str, Any],
    session_id: str
) -> concurrent.futures.Future:
    """Start background exec for sessionId-linked hooks.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace
        pre_hook: Pre-hook configuration
        post_hook: Post-hook configuration
        session_id: Session identifier

    Returns:
        Future object for background task
    """
    from kubernetes.stream import stream

    pod_name = pre_hook.get('pod')
    container = pre_hook.get('container')

    # Build combined command
    command = build_linked_session_command(pre_hook, post_hook, session_id)

    def execute_linked_session():
        """Background task: execute linked session."""
        v1 = client.CoreV1Api(api_client)

        exec_kwargs: dict[str, Any] = {
            'name': pod_name,
            'namespace': namespace,
            'command': command,
            'stderr': True,
            'stdout': True,
            'stdin': False,
            'tty': False,
            '_preload_content': False
        }

        if container:
            exec_kwargs['container'] = container

        try:
            resp = stream(v1.connect_get_namespaced_pod_exec, **exec_kwargs)

            # Stream output
            while resp.is_open():
                resp.update(timeout=1)
                if resp.peek_stdout():
                    print(resp.read_stdout(), end='', flush=True)
                if resp.peek_stderr():
                    print(resp.read_stderr(), end='', flush=True)

            exit_code = resp.returncode
            if exit_code != 0:
                raise Exception(f"Linked session {session_id} failed with exit code {exit_code}")

            return {'sessionId': session_id, 'success': True}

        except Exception as e:
            print(f"❌ [SessionId: {session_id}] Linked session failed: {e}", file=sys.stderr)
            raise

    # Submit to thread pool
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(execute_linked_session)
    return future


def write_signal_file(
    api_client: client.ApiClient,
    namespace: str,
    pod_name: str,
    container: str | None,
    session_id: str
) -> None:
    """Write signal file to trigger post-hook in linked session.

    Args:
        api_client: Kubernetes API client
        namespace: Namespace
        pod_name: Pod name
        container: Optional container name
        session_id: Session identifier

    Raises:
        Exception: If signal file write fails
    """
    from kubernetes.stream import stream

    signal_file = f"/tmp/kbb-signal-{session_id}"
    command = ['/bin/sh', '-c', f'touch {signal_file} || {{ echo "Failed to write signal file"; exit 1; }}']

    v1 = client.CoreV1Api(api_client)

    exec_kwargs: dict[str, Any] = {
        'name': pod_name,
        'namespace': namespace,
        'command': command,
        'stderr': True,
        'stdout': True,
        'stdin': False,
        'tty': False,
        '_preload_content': False
    }

    if container:
        exec_kwargs['container'] = container

    try:
        resp = stream(v1.connect_get_namespaced_pod_exec, **exec_kwargs)

        # Read output
        stdout_output = ''
        stderr_output = ''
        while resp.is_open():
            resp.update(timeout=1)
            if resp.peek_stdout():
                stdout_output += resp.read_stdout()
            if resp.peek_stderr():
                stderr_output += resp.read_stderr()

        exit_code = resp.returncode
        if exit_code != 0:
            raise Exception(
                f"Failed to write signal file {signal_file} (exit code {exit_code})\n"
                f"Stdout: {stdout_output}\nStderr: {stderr_output}\n"
                f"Hint: If using sessionId, ensure /tmp is writable in pod '{pod_name}'"
            )

        print(f"✅ [SessionId: {session_id}] Signal file written, post-hook will execute")

    except Exception as e:
        print(f"❌ [SessionId: {session_id}] Failed to write signal file: {e}", file=sys.stderr)
        raise


def log_msg(msg: str) -> None:
    """Log message to stdout."""
    print(msg)


def main() -> None:
    """Main execution flow."""
    global _config, _namespace, _api_client

    # Register signal handler for graceful shutdown
    signal.signal(signal.SIGTERM, lambda s, f: cleanup_post_hooks())

    args = parse_args()
    cfg = load_config(args.config)
    _config = cfg

    namespace = cfg.get("namespace")
    if not namespace:
        log_msg("❌ Config missing required field: namespace")
        sys.exit(2)
    _namespace = namespace

    test_mode = args.test

    custom_api, api_client = init_clients()
    _api_client = api_client

    log_msg(f"🔧 Using namespace: {namespace}")

    if test_mode:
        log_msg("⏱️  TEST MODE: Waiting 5 seconds before starting snapshots...")
        time.sleep(5)
        log_msg("✅ TEST MODE: Delay complete, proceeding with snapshots")

    snapshot_config = cfg.get("snapshots", {})
    pvcs = snapshot_config.get("pvcs", [])
    retention = snapshot_config.get("retention", {})

    if not pvcs:
        print("⚠️  No PVCs configured for snapshot", file=sys.stderr)
        sys.exit(0)

    # Collect all pre-hooks and post-hooks from all PVCs
    all_pre_hooks = []
    all_post_hooks = []
    for pvc_cfg in pvcs:
        pre_hooks = pvc_cfg.get("hooks", {}).get("pre", [])
        post_hooks = pvc_cfg.get("hooks", {}).get("post", [])
        all_pre_hooks.extend(pre_hooks)
        all_post_hooks.extend(post_hooks)

    # Group hooks by sessionId
    session_map: dict[str, dict[str, Any]] = {}  # sessionId -> {pre: hook, post: hook, pod: str, container: str|None}
    non_linked_pre_hooks = []
    non_linked_post_hooks = []

    # Build session map
    for hook in all_pre_hooks:
        session_id = hook.get('sessionId')
        if session_id:
            if session_id not in session_map:
                session_map[session_id] = {}
            session_map[session_id]['pre'] = hook
            session_map[session_id]['pod'] = hook.get('pod')
            session_map[session_id]['container'] = hook.get('container')
        else:
            non_linked_pre_hooks.append(hook)

    for hook in all_post_hooks:
        session_id = hook.get('sessionId')
        if session_id:
            if session_id not in session_map:
                session_map[session_id] = {}
            session_map[session_id]['post'] = hook
        else:
            non_linked_post_hooks.append(hook)

    # Validate session map (pre and post must both exist)
    for session_id, session_data in session_map.items():
        if 'pre' not in session_data or 'post' not in session_data:
            print(f"❌ SessionId '{session_id}' missing pre or post hook", file=sys.stderr)
            sys.exit(2)

    snapshot_failed = False
    linked_session_futures: dict[str, concurrent.futures.Future] = {}

    try:
        # Step 1: Start linked sessions and run non-linked pre-hooks
        if session_map or non_linked_pre_hooks:
            print(f"\n{'='*60}")
            print("🔄 Starting pre-hooks")
            print(f"{'='*60}\n")

            # Start linked sessions in background
            for session_id, session_data in session_map.items():
                print(f"🔗 Starting linked session: {session_id}")
                future = start_linked_session(
                    api_client,
                    namespace,
                    session_data['pre'],
                    session_data['post'],
                    session_id
                )
                linked_session_futures[session_id] = future

            # Poll for pre-done checkpoint files (blocks until all pre-hooks complete)
            for session_id, session_data in session_map.items():
                pre_done_file = f"/tmp/kbb-pre-done-{session_id}"
                poll_for_checkpoint_file(
                    api_client,
                    namespace,
                    session_data['pod'],
                    session_data['container'],
                    pre_done_file,
                    session_id
                )

            # Execute non-linked pre-hooks
            if non_linked_pre_hooks:
                transformed_pre_hooks = transform_hooks_to_common_format(non_linked_pre_hooks)
                execute_hooks(api_client, namespace, transformed_pre_hooks, mode="pre")

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
                    _ = future.result()  # Wait for completion, result unused
                except Exception as exc:
                    pvc_name = pvc_cfg.get("name", "unknown")
                    print(f"❌ Failed to create snapshot for {pvc_name}: {exc}", file=sys.stderr)
                    snapshot_failed = True

        # Step 3: Prune old snapshots
        if retention:
            print(f"\n{'='*60}")
            print("🗑️  Pruning old snapshots")
            print(f"{'='*60}\n")

            for pvc_cfg in pvcs:
                pvc_name = pvc_cfg.get("name")
                if pvc_name:
                    prune_snapshots_tiered(custom_api, pvc_name, retention, namespace)

        # Step 4: Trigger linked sessions' post-hooks by writing signal files
        if session_map:
            print(f"\n{'='*60}")
            print(f"🔄 Triggering post-hooks for {len(session_map)} linked session(s)")
            print(f"{'='*60}\n")

            for session_id, session_data in session_map.items():
                write_signal_file(
                    api_client,
                    namespace,
                    session_data['pod'],
                    session_data['container'],
                    session_id
                )

            # Wait 2 seconds for post-hooks to start
            print("⏳ Waiting 2s for post-hooks to start...")
            time.sleep(2)

            # Verify post-started checkpoint files exist (ensures sessions still alive)
            for session_id, session_data in session_map.items():
                post_started_file = f"/tmp/kbb-post-started-{session_id}"
                if not check_file_exists(
                    api_client,
                    namespace,
                    session_data['pod'],
                    session_data['container'],
                    post_started_file
                ):
                    print(
                        f"❌ [SessionId: {session_id}] Post-started checkpoint not found - "
                        f"linked session may have died",
                        file=sys.stderr
                    )
                    snapshot_failed = True
                else:
                    print(f"✅ [SessionId: {session_id}] Post-hook started successfully")

        # Step 5: Wait for linked session futures to complete
        if linked_session_futures:
            print(f"\n{'='*60}")
            print(f"⏳ Waiting for {len(linked_session_futures)} linked session(s) to complete")
            print(f"{'='*60}\n")

            for session_id, future in linked_session_futures.items():
                try:
                    result = future.result()  # Block until session completes
                    print(f"✅ [SessionId: {session_id}] Linked session completed: {result}")
                except Exception as exc:
                    print(f"❌ [SessionId: {session_id}] Linked session failed: {exc}", file=sys.stderr)
                    snapshot_failed = True

    except Exception as exc:
        print(f"\n❌ Error during snapshot process: {exc}", file=sys.stderr)
        snapshot_failed = True

    finally:
        # Step 6: ALWAYS run non-linked post-hooks (even on failure)
        # Linked post-hooks are handled by their background sessions
        if non_linked_post_hooks:
            try:
                # Transform hooks to common library format
                transformed_post_hooks = transform_hooks_to_common_format(non_linked_post_hooks)

                print(f"\n{'='*60}")
                print(f"🔄 Running non-linked post-hooks ({len(transformed_post_hooks)} total)")
                print(f"{'='*60}\n")

                # Execute with common hooks library
                execute_hooks(api_client, namespace, transformed_post_hooks, mode="post")
            except Exception as exc:
                print(f"❌ Non-linked post-hooks failed: {exc}", file=sys.stderr)
                snapshot_failed = True

    if snapshot_failed:
        print("\n❌ Snapshot process completed with errors", file=sys.stderr)
        sys.exit(1)

    print("\n✅ Snapshot process completed successfully!")


if __name__ == "__main__":
    main()
