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
            print(f"No PVCs configured for snapshot in app '{args.app}'")
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
            print(f"Error querying VolumeSnapshots: {e}", file=sys.stderr)
            sys.exit(1)

        # Filter by source PVC
        matching_snapshots: list[dict[str, Any]] = []
        for snapshot in snapshots_response.get('items', []):
            source_pvc = snapshot.get('spec', {}).get('source', {}).get('persistentVolumeClaimName')
            if source_pvc in pvc_names:
                matching_snapshots.append(snapshot)

        # Display results
        if not matching_snapshots:
            print(f"No snapshots found for app '{args.app}' in namespace '{args.namespace}'")
            return

        # Sort by creation time (newest first)
        matching_snapshots.sort(
            key=lambda s: s.get('metadata', {}).get('creationTimestamp', ''),
            reverse=True
        )

        # Print table
        print(f"\nSnapshots for {args.app} ({len(matching_snapshots)} found):\n")
        print(f"{'NAME':<50} {'PVC':<30} {'CREATED':<25} {'READY':<10}")
        print("-" * 120)

        for snapshot in matching_snapshots:
            name = snapshot['metadata']['name']
            pvc = snapshot['spec']['source']['persistentVolumeClaimName']
            created = snapshot['metadata']['creationTimestamp']
            ready = snapshot.get('status', {}).get('readyToUse', False)

            print(f"{name:<50} {pvc:<30} {created:<25} {'Yes' if ready else 'No':<10}")

        print()  # Empty line after table

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def restore_snapshot(args: argparse.Namespace) -> None:
    """Restore from VolumeSnapshot with pre/post hooks.

    Full workflow:
    1. Load config and restore hooks
    2. Execute pre-hooks (fail-fast)
    3. Find snapshot and extract source PVC
    4. Determine target PVC (explicit or in-place restore)
    5. Create clone PVC from snapshot
    6. Spawn rsync pod to copy data (120s max)
    7. Execute post-hooks (best-effort)
    8. Cleanup clone PVC

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

        # Extract image config for rsync pod
        pod_config = config.get('pod', {})
        default_repo = 'ghcr.io/frederikb96/kube-borg-backup/backup-runner'
        image_repository = pod_config.get('image', {}).get('repository', default_repo)
        image_tag = pod_config.get('image', {}).get('tag', 'latest')

        # Step 2: Execute pre-hooks (fail-fast)
        pre_hooks = restore_config.get('preHooks', [])
        if pre_hooks:
            print("Executing pre-hooks...")
            v1, _ = load_kube_client()
            api_client = v1.api_client
            result = execute_hooks(api_client, args.namespace, pre_hooks, mode='pre')
            if not result['success']:
                print(f"Pre-hooks failed: {result['failed']}", file=sys.stderr)
                sys.exit(1)
            print("Pre-hooks completed successfully")

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
            print(f"Error: VolumeSnapshot '{args.snapshot_id}' not found in namespace '{args.namespace}'", file=sys.stderr)
            print(f"Details: {e}", file=sys.stderr)
            sys.exit(1)

        # Verify snapshot is ready
        if not snapshot.get('status', {}).get('readyToUse', False):
            print(f"Error: VolumeSnapshot '{args.snapshot_id}' is not ready to use", file=sys.stderr)
            sys.exit(1)

        # Extract source PVC name
        source_pvc = snapshot.get('spec', {}).get('source', {}).get('persistentVolumeClaimName')
        if not source_pvc:
            print(f"Error: Could not determine source PVC from snapshot '{args.snapshot_id}'", file=sys.stderr)
            sys.exit(1)

        print(f"Found snapshot '{args.snapshot_id}' from source PVC '{source_pvc}'")

        # Step 4: Determine target PVC
        target_pvc = args.pvc if args.pvc else source_pvc
        print(f"Target PVC: {target_pvc}")

        # Step 5: Create clone PVC
        clone_pvc_name = f"{source_pvc}-restore-{int(time.time())}"
        print(f"Creating clone PVC '{clone_pvc_name}' from snapshot...")

        clone_result = create_clone_pvc(
            namespace=args.namespace,
            snapshot_name=args.snapshot_id,
            clone_pvc_name=clone_pvc_name,
            storage_class='longhorn-temp'  # Use Immediate binding for testing
        )
        print(f"Clone PVC created: {clone_result['name']} (binding mode: {clone_result['binding_mode']})")

        # Step 6: Spawn rsync pod (120s max)
        print(f"Spawning rsync pod to copy data to '{target_pvc}'...")
        try:
            rsync_result = spawn_rsync_pod(
                namespace=args.namespace,
                source_pvc_name=clone_pvc_name,
                target_pvc_name=target_pvc,
                timeout=120,  # CRITICAL: Never wait longer than 120s
                image_repository=image_repository,
                image_tag=image_tag
            )
            if not rsync_result['success']:
                raise Exception(f"Rsync failed: {rsync_result['logs']}")
            print("Data restored successfully")

        except Exception as e:
            print(f"Restore failed: {e}", file=sys.stderr)
            # Cleanup clone PVC
            _cleanup_clone_pvc(v1, args.namespace, clone_pvc_name)
            sys.exit(1)

        # Step 7: Execute post-hooks (best-effort)
        post_hooks = restore_config.get('postHooks', [])
        if post_hooks:
            print("Executing post-hooks...")
            api_client = v1.api_client
            result = execute_hooks(api_client, args.namespace, post_hooks, mode='post')
            if not result['success']:
                print(f"Warning: Some post-hooks failed: {result['failed']}", file=sys.stderr)
            else:
                print("Post-hooks completed successfully")

        # Step 8: Cleanup clone PVC
        _cleanup_clone_pvc(v1, args.namespace, clone_pvc_name)

        print(f"\n✅ Restore complete: snapshot '{args.snapshot_id}' → PVC '{target_pvc}'")

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
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
        print(f"Cleaned up clone PVC: {pvc_name}")
    except Exception:
        pass  # Ignore cleanup errors
