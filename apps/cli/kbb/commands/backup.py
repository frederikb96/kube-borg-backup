"""Borg backup restore commands."""

import argparse


def handle_backup(args: argparse.Namespace) -> None:
    """Handle backup subcommand."""
    if args.backup_command == 'list':
        list_borg_archives(args)
    elif args.backup_command == 'restore':
        restore_borg_archive(args)


def list_borg_archives(args: argparse.Namespace) -> None:
    """List borg archives by spawning borg-list pod."""
    print(f"[STUB] Listing borg archives for app '{args.app}' in namespace '{args.namespace}'")
    print("TODO: Spawn borg-list pod, parse JSON output")
    # Implementation in Phase 6


def restore_borg_archive(args: argparse.Namespace) -> None:
    """Restore from borg archive."""
    print(f"[STUB] Restoring archive '{args.archive_id}' for app '{args.app}'")
    if args.pvc:
        print(f"Target PVC override: {args.pvc}")
    print("TODO: Implement borg restore workflow")
    # Implementation in Phase 6
