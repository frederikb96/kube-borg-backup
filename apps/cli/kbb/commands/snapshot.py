"""Snapshot restore commands."""

import argparse
import sys
from typing import Any
from kubernetes import client
from kbb.utils import find_app_config, load_kube_client


def handle_snap(args: argparse.Namespace) -> None:
    """Handle snapshot subcommand."""
    if args.snap_command == 'list':
        list_snapshots(args)
    elif args.snap_command == 'restore':
        restore_snapshot(args)


def list_snapshots(args: argparse.Namespace) -> None:
    """List snapshots for app's PVCs."""
    try:
        # Load config to get PVC names
        config = find_app_config(args.namespace, args.app, args.release, 'snapshot')

        # Extract PVC names from config
        pvc_names: list[str] = []
        if 'snapshots' in config and 'pvcs' in config['snapshots']:
            for pvc_config in config['snapshots']['pvcs']:
                pvc_names.append(pvc_config['name'])

        if not pvc_names:
            print(f"No PVCs configured for snapshot in app '{args.app}'", flush=True)
            return

        # Query VolumeSnapshots
        _, custom_api = load_kube_client()

        try:
            snapshots_response = custom_api.list_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=args.namespace,
                plural="volumesnapshots"
            )
        except client.exceptions.ApiException as e:
            print(f"Error querying VolumeSnapshots: {e}", file=sys.stderr, flush=True)
            sys.exit(1)

        # Filter by source PVC
        matching_snapshots: list[dict[str, Any]] = []
        for snapshot in snapshots_response.get('items', []):
            source_pvc = snapshot.get('spec', {}).get('source', {}).get('persistentVolumeClaimName')
            if source_pvc in pvc_names:
                matching_snapshots.append(snapshot)

        # Display results
        if not matching_snapshots:
            print(f"No snapshots found for app '{args.app}' in namespace '{args.namespace}'", flush=True)
            return

        # Sort by creation time (newest first)
        matching_snapshots.sort(
            key=lambda s: s.get('metadata', {}).get('creationTimestamp', ''),
            reverse=True
        )

        # Print table
        print(f"\nSnapshots for {args.app} ({len(matching_snapshots)} found):\n", flush=True)
        print(f"{'NAME':<50} {'PVC':<30} {'CREATED':<25} {'READY':<10}", flush=True)
        print("-" * 120, flush=True)

        for snapshot in matching_snapshots:
            name = snapshot['metadata']['name']
            pvc = snapshot['spec']['source']['persistentVolumeClaimName']
            created = snapshot['metadata']['creationTimestamp']
            ready = snapshot.get('status', {}).get('readyToUse', False)

            print(f"{name:<50} {pvc:<30} {created:<25} {'Yes' if ready else 'No':<10}", flush=True)

        print(flush=True)  # Empty line after table

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


def restore_snapshot(args: argparse.Namespace) -> None:
    """Restore from VolumeSnapshot with pre/post hooks.

    Full workflow:
    1. Load config and restore hooks
    2. Execute pre-hooks (fail-fast)
    3. Find snapshot and extract source PVC
    4. Extract storage class from borgbackup config (same as used during backup)
    5. Determine target PVC (explicit or in-place restore)
    6. Create clone PVC from snapshot
    7. Spawn rsync pod to copy data (waits indefinitely for completion)
    8. Execute post-hooks (best-effort)
    9. Cleanup clone PVC

    Args:
        args: CLI arguments with namespace, app, release, snapshot_id, optional pvc
    """
    import time
    from kbb.restore_helpers import create_clone_pvc, spawn_rsync_pod
    from kbb.hooks import execute_hooks

    try:
        # Step 1: Load config
        config = find_app_config(args.namespace, args.app, args.release, config_type='snapshot')
        restore_config = config.get('restore', {})

        # Step 2: Execute pre-hooks (fail-fast)
        pre_hooks = restore_config.get('preHooks', [])
        if pre_hooks:
            print("Executing pre-hooks...", flush=True)
            v1, _ = load_kube_client()
            api_client = v1.api_client
            result = execute_hooks(api_client, args.namespace, pre_hooks, mode='pre')
            if not result['success']:
                print(f"Pre-hooks failed: {result['failed']}", file=sys.stderr, flush=True)
                sys.exit(1)
            print("Pre-hooks completed successfully", flush=True)

        # Step 3: Find snapshot
        v1, custom_api = load_kube_client()

        try:
            snapshot = custom_api.get_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=args.namespace,
                plural="volumesnapshots",
                name=args.snapshot_id
            )
        except client.exceptions.ApiException as e:
            print(
                f"Error: VolumeSnapshot '{args.snapshot_id}' not found in namespace '{args.namespace}'",
                file=sys.stderr,
                flush=True
            )
            print(f"Details: {e}", file=sys.stderr, flush=True)
            sys.exit(1)

        # Verify snapshot is ready
        if not snapshot.get('status', {}).get('readyToUse', False):
            print(f"Error: VolumeSnapshot '{args.snapshot_id}' is not ready to use", file=sys.stderr, flush=True)
            sys.exit(1)

        # Extract source PVC name
        source_pvc = snapshot.get('spec', {}).get('source', {}).get('persistentVolumeClaimName')
        if not source_pvc:
            print(
                f"Error: Could not determine source PVC from snapshot '{args.snapshot_id}'",
                file=sys.stderr, flush=True
            )
            sys.exit(1)

        print(f"Found snapshot '{args.snapshot_id}' from source PVC '{source_pvc}'", flush=True)

        # Step 4: Extract storage class from borgbackup config
        # This ensures we use the same storage class that was used during backup clone creation
        try:
            borg_config = find_app_config(args.namespace, args.app, args.release, config_type='borg')
            storage_class = None

            # Find the backup entry that matches our source PVC
            if 'backups' in borg_config:
                for backup in borg_config['backups']:
                    if backup.get('pvc') == source_pvc:
                        storage_class = backup.get('class')
                        break

            if not storage_class:
                print(
                    f"Warning: Could not find storage class in borgbackup config for PVC '{source_pvc}'",
                    file=sys.stderr,
                    flush=True
                )
                print("Clone PVC will use default storage class", flush=True)
        except Exception as e:
            print(
                f"Warning: Could not load borgbackup config: {e}",
                file=sys.stderr,
                flush=True
            )
            print("Clone PVC will use default storage class", flush=True)
            storage_class = None

        # Step 5: Determine target PVC
        target_pvc = args.pvc if args.pvc else source_pvc
        print(f"Target PVC: {target_pvc}", flush=True)

        # Step 6: Create clone PVC
        clone_pvc_name = f"{source_pvc}-restore-{int(time.time())}"
        print(f"Creating clone PVC '{clone_pvc_name}' from snapshot...", flush=True)
        if storage_class:
            print(f"Using storage class from borgbackup config: {storage_class}", flush=True)

        clone_result = create_clone_pvc(
            namespace=args.namespace,
            snapshot_name=args.snapshot_id,
            clone_pvc_name=clone_pvc_name,
            storage_class=storage_class
        )
        print(f"Clone PVC created: {clone_result['name']} (binding mode: {clone_result['binding_mode']})", flush=True)

        # Step 7: Spawn rsync pod (no timeout - waits indefinitely)
        print(f"Spawning rsync pod to copy data to '{target_pvc}'...", flush=True)
        try:
            # Extract restore pod image config (REQUIRED)
            pod_config = restore_config.get('pod', {})
            image_config = pod_config.get('image', {})
            if not image_config.get('repository') or not image_config.get('tag'):
                raise ValueError("restore.pod.image.repository and restore.pod.image.tag are REQUIRED in config")

            rsync_result = spawn_rsync_pod(
                namespace=args.namespace,
                source_pvc_name=clone_pvc_name,
                target_pvc_name=target_pvc,
                image_repository=image_config['repository'],
                image_tag=image_config['tag'],
                pod_name=None  # Auto-generate
            )
            if not rsync_result['success']:
                raise Exception("Rsync failed")
            print("Data restored successfully", flush=True)

        except Exception as e:
            print(f"Restore failed: {e}", file=sys.stderr, flush=True)
            # Cleanup clone PVC
            _cleanup_clone_pvc(v1, args.namespace, clone_pvc_name)
            sys.exit(1)

        # Step 7: Execute post-hooks (best-effort)
        post_hooks = restore_config.get('postHooks', [])
        if post_hooks:
            print("Executing post-hooks...", flush=True)
            api_client = v1.api_client
            result = execute_hooks(api_client, args.namespace, post_hooks, mode='post')
            if not result['success']:
                print(f"Warning: Some post-hooks failed: {result['failed']}", file=sys.stderr, flush=True)
            else:
                print("Post-hooks completed successfully", flush=True)

        # Step 8: Cleanup clone PVC
        _cleanup_clone_pvc(v1, args.namespace, clone_pvc_name)

        print(f"\n✅ Restore complete: snapshot '{args.snapshot_id}' → PVC '{target_pvc}'", flush=True)

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


def _cleanup_clone_pvc(v1: client.CoreV1Api, namespace: str, pvc_name: str) -> None:
    """Delete clone PVC, ignore errors.

    Args:
        v1: Kubernetes CoreV1Api instance
        namespace: Namespace of the PVC
        pvc_name: PVC name to delete
    """
    try:
        v1.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
        print(f"Cleaned up clone PVC: {pvc_name}", flush=True)
    except Exception:
        pass  # Ignore cleanup errors
