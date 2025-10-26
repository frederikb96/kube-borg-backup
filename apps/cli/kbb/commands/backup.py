"""Borg backup restore commands."""

import argparse
import json
import sys
import time
from typing import Any
import yaml
from kubernetes import client
from kbb.utils import find_app_config, load_kube_client


def handle_backup(args: argparse.Namespace) -> None:
    """Handle backup subcommand."""
    if args.backup_command == 'list':
        list_borg_archives(args)
    elif args.backup_command == 'restore':
        restore_borg_archive(args)


def list_borg_archives(args: argparse.Namespace) -> None:
    """List borg archives by spawning borg-list pod.

    Args:
        args: Namespace with namespace, app, and release attributes
    """
    try:
        # Load config
        config = find_app_config(args.namespace, args.app, args.release, config_type='borg')

        # Extract borg config for pod
        borg_config = {
            'borgRepo': config.get('borgRepo'),
            'borgPassphrase': config.get('borgPassphrase'),
            'sshPrivateKey': config.get('sshPrivateKey'),
        }

        # Validate required fields
        missing = [k for k, v in borg_config.items() if not v]
        if missing:
            print(f"Error: Config missing required fields: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)

        # Create ephemeral config Secret
        v1, _ = load_kube_client()
        config_yaml = yaml.dump(borg_config)
        secret_name = f"kbb-{args.app}-list-{int(time.time())}"

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=args.namespace),
            string_data={'config.yaml': config_yaml}
        )

        try:
            v1.create_namespaced_secret(args.namespace, secret)
        except client.exceptions.ApiException as e:
            print(f"Error creating config Secret: {e}", file=sys.stderr)
            sys.exit(1)

        # Spawn pod with list.py
        pod_name = f"kbb-{args.app}-list-{int(time.time())}"
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=args.namespace,
                labels={'app': 'kube-borg-backup', 'operation': 'list'}
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name='borg-list',
                        image='ghcr.io/frederikb96/kube-borg-backup/backup-runner:dev',
                        command=['python3', '/app/list.py'],
                        image_pull_policy='Always',
                        volume_mounts=[
                            client.V1VolumeMount(
                                name='config',
                                mount_path='/config',
                                read_only=True
                            )
                        ]
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name='config',
                        secret=client.V1SecretVolumeSource(secret_name=secret_name)
                    )
                ],
                restart_policy='Never'
            )
        )

        try:
            v1.create_namespaced_pod(args.namespace, pod)
        except client.exceptions.ApiException as e:
            print(f"Error creating pod: {e}", file=sys.stderr)
            # Cleanup secret
            try:
                v1.delete_namespaced_secret(secret_name, args.namespace)
            except Exception:
                pass
            sys.exit(1)

        # Wait for pod completion (timeout 120s)
        timeout = 120
        start_time = time.time()
        while True:
            if time.time() - start_time > timeout:
                print("Error: Pod did not complete within timeout", file=sys.stderr)
                cleanup_list_resources(v1, args.namespace, pod_name, secret_name)
                sys.exit(1)

            try:
                pod_status = v1.read_namespaced_pod_status(pod_name, args.namespace)
                phase = pod_status.status.phase
                if phase == 'Succeeded':
                    break
                elif phase == 'Failed':
                    print("Error: Pod failed", file=sys.stderr)
                    # Show logs for debugging
                    try:
                        logs = v1.read_namespaced_pod_log(pod_name, args.namespace)
                        print(f"Pod logs:\n{logs}", file=sys.stderr)
                    except Exception:
                        pass
                    cleanup_list_resources(v1, args.namespace, pod_name, secret_name)
                    sys.exit(1)
            except client.exceptions.ApiException as e:
                print(f"Error checking pod status: {e}", file=sys.stderr)
                cleanup_list_resources(v1, args.namespace, pod_name, secret_name)
                sys.exit(1)

            time.sleep(2)

        # Read logs
        try:
            logs = v1.read_namespaced_pod_log(pod_name, args.namespace)
        except client.exceptions.ApiException as e:
            print(f"Error reading pod logs: {e}", file=sys.stderr)
            cleanup_list_resources(v1, args.namespace, pod_name, secret_name)
            sys.exit(1)

        # Parse JSON from logs
        # The list.py script outputs JSON to stdout, logs go to stderr via logging
        # Find the JSON block in the mixed log output
        try:
            # Find start of JSON output (line with just '{')
            lines = logs.split('\n')
            json_start = -1
            for i, line in enumerate(lines):
                if line.strip() == '{':
                    json_start = i
                    break

            if json_start == -1:
                print("Error: No JSON output found in pod logs", file=sys.stderr)
                print(f"Raw logs:\n{logs}", file=sys.stderr)
                cleanup_list_resources(v1, args.namespace, pod_name, secret_name)
                sys.exit(1)

            # Extract JSON block (from '{' to matching '}')
            json_str = '\n'.join(lines[json_start:])
            archive_data: dict[str, Any] = json.loads(json_str)
        except json.JSONDecodeError as e:
            print(f"Error parsing JSON from logs: {e}", file=sys.stderr)
            print(f"Raw logs:\n{logs}", file=sys.stderr)
            cleanup_list_resources(v1, args.namespace, pod_name, secret_name)
            sys.exit(1)

        # Cleanup pod and secret
        cleanup_list_resources(v1, args.namespace, pod_name, secret_name)

        # Display results
        archives = archive_data.get('archives', [])
        repository = archive_data.get('repository', 'Unknown')

        print(f"\nBorg archives for {args.app} ({len(archives)} found):")
        print(f"Repository: {repository}\n")

        if not archives:
            print("No archives found.")
            return

        # Print table
        print(f"{'ARCHIVE':<60} {'CREATED':<25} {'ID':<15}")
        print("-" * 105)

        for archive in archives:
            name = archive.get('name', 'N/A')
            created = archive.get('time', 'N/A')
            archive_id = archive.get('id', 'N/A')

            print(f"{name:<60} {created:<25} {archive_id:<15}")

        print()  # Empty line after table

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def cleanup_list_resources(v1: client.CoreV1Api, namespace: str, pod_name: str, secret_name: str) -> None:
    """Cleanup pod and secret after list operation.

    Args:
        v1: CoreV1Api client
        namespace: Kubernetes namespace
        pod_name: Name of pod to delete
        secret_name: Name of secret to delete
    """
    try:
        v1.delete_namespaced_pod(pod_name, namespace)
    except Exception:
        pass  # Ignore cleanup errors

    try:
        v1.delete_namespaced_secret(secret_name, namespace)
    except Exception:
        pass  # Ignore cleanup errors


def restore_borg_archive(args: argparse.Namespace) -> None:
    """Restore from borg archive."""
    print(f"[STUB] Restoring archive '{args.archive_id}' for app '{args.app}'")
    if args.pvc:
        print(f"Target PVC override: {args.pvc}")
    print("TODO: Implement borg restore workflow")
    # Implementation in Phase 10+
