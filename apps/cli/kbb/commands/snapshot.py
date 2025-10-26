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
    """Restore from snapshot."""
    print(f"[STUB] Restoring snapshot '{args.snapshot_id}' for app '{args.app}'")
    if args.pvc:
        print(f"Target PVC override: {args.pvc}")
    print("TODO: Implement snapshot restore workflow")
    # Implementation in Phase 5
