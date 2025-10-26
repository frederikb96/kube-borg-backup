#!/usr/bin/env python3
"""kbb - CLI tool for kube-borg-backup restore operations."""

import argparse
import sys


def create_parser() -> argparse.ArgumentParser:
    """Create argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog='kbb',
        description='Kubernetes Borg Backup restore CLI'
    )

    # Global options (can appear anywhere, kubectl-style)
    parser.add_argument(
        '-n', '--namespace',
        required=True,
        help='Kubernetes namespace'
    )
    parser.add_argument(
        '-a', '--app',
        required=True,
        help='Application name'
    )

    # Subcommands
    subparsers = parser.add_subparsers(dest='command', required=True)

    # snap subcommand
    snap = subparsers.add_parser('snap', help='Snapshot operations')
    snap_sub = snap.add_subparsers(dest='snap_command', required=True)

    snap_sub.add_parser('list', help='List snapshots')

    snap_restore = snap_sub.add_parser('restore', help='Restore from snapshot')
    snap_restore.add_argument('snapshot_id', help='Snapshot ID to restore')
    snap_restore.add_argument('--pvc', help='Override target PVC name')

    # backup subcommand
    backup = subparsers.add_parser('backup', help='Borg backup operations')
    backup_sub = backup.add_subparsers(dest='backup_command', required=True)

    backup_sub.add_parser('list', help='List borg archives')

    backup_restore = backup_sub.add_parser('restore', help='Restore from archive')
    backup_restore.add_argument('archive_id', help='Archive ID to restore')
    backup_restore.add_argument('--pvc', help='Override target PVC name')

    return parser


def main() -> None:
    """CLI entry point."""
    parser = create_parser()
    args = parser.parse_args()

    # Import command handlers
    if args.command == 'snap':
        from kbb.commands.snapshot import handle_snap
        handle_snap(args)
    elif args.command == 'backup':
        from kbb.commands.backup import handle_backup
        handle_backup(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == '__main__':
    main()
