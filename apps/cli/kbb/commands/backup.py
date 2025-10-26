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
    """Restore from borg archive with FUSE mount.

    Full workflow:
    1. Load config and restore hooks
    2. Execute pre-hooks (fail-fast)
    3. Determine target PVC (explicit or first backup PVC)
    4. Create ephemeral config Secret
    5. Spawn borg-restore pod (120s max)
    6. Execute post-hooks (best-effort)
    7. Cleanup pod + secret

    Args:
        args: CLI arguments with namespace, app, release, archive_id, optional pvc
    """
    from kbb.hooks import execute_hooks

    try:
        # Step 1: Load config
        config = find_app_config(args.namespace, args.app, args.release, config_type='borg')
        restore_config = config.get('restore', {})

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

        # Step 3: Determine target PVC
        v1, _ = load_kube_client()

        if args.pvc:
            target_pvc = args.pvc
        else:
            # Use first backup's PVC name from config
            backups = config.get('backups', [])
            if not backups:
                print("Error: No backups configured in config", file=sys.stderr)
                sys.exit(1)
            target_pvc = backups[0]['pvc']

        print(f"Target PVC: {target_pvc}")

        # Step 4: Create ephemeral config Secret
        secret_name = f"kbb-{args.app}-restore-{int(time.time())}"

        restore_config_data = {
            'borgRepo': config['borgRepo'],
            'borgPassphrase': config['borgPassphrase'],
            'sshPrivateKey': config['sshPrivateKey'],
            'archiveName': args.archive_id,
            'targetPath': '/target'  # Standard rsync target
        }

        secret = client.V1Secret(
            metadata=client.V1ObjectMeta(name=secret_name, namespace=args.namespace),
            string_data={'config.yaml': yaml.dump(restore_config_data)}
        )

        try:
            v1.create_namespaced_secret(args.namespace, secret)
            print(f"Created ephemeral config Secret: {secret_name}")
        except client.exceptions.ApiException as e:
            print(f"Error creating config Secret: {e}", file=sys.stderr)
            sys.exit(1)

        # Step 5: Spawn borg-restore pod (120s timeout)
        pod_name = f"kbb-{args.app}-restore-{int(time.time())}"

        # Get cache PVC name
        cache_pvc = config.get('cachePVC', f"kbb-{args.app}-borg-cache")

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=args.namespace,
                labels={'app': 'kube-borg-backup', 'operation': 'restore'}
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name='borg-restore',
                        image='ghcr.io/frederikb96/kube-borg-backup/backup-runner:dev',
                        command=['python3', '/app/restore.py'],
                        image_pull_policy='Always',
                        security_context=client.V1SecurityContext(privileged=True),  # FUSE needs privileged
                        volume_mounts=[
                            client.V1VolumeMount(name='config', mount_path='/config', read_only=True),
                            client.V1VolumeMount(name='cache', mount_path='/root/.cache/borg'),
                            client.V1VolumeMount(name='target', mount_path='/target')
                        ]
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name='config',
                        secret=client.V1SecretVolumeSource(secret_name=secret_name)
                    ),
                    client.V1Volume(
                        name='cache',
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=cache_pvc
                        )
                    ),
                    client.V1Volume(
                        name='target',
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=target_pvc
                        )
                    )
                ],
                restart_policy='Never'
            )
        )

        try:
            v1.create_namespaced_pod(args.namespace, pod)
            print(f"Borg restore pod '{pod_name}' created")

            # Wait for completion (120s max)
            start_time = time.time()
            while time.time() - start_time < 120:
                pod_status = v1.read_namespaced_pod_status(pod_name, args.namespace)
                phase = pod_status.status.phase

                if phase == 'Succeeded':
                    # Get logs
                    logs = v1.read_namespaced_pod_log(pod_name, args.namespace)
                    print("Restore completed successfully")
                    print(f"Logs:\n{logs}")
                    break
                elif phase == 'Failed':
                    logs = v1.read_namespaced_pod_log(pod_name, args.namespace)
                    raise Exception(f"Restore pod failed:\n{logs}")

                time.sleep(2)
            else:
                # Timeout after 120s
                raise Exception(f"Restore pod timeout after 120s")

        except Exception as e:
            print(f"Restore failed: {e}", file=sys.stderr)
            # Cleanup
            _cleanup_restore_resources(v1, args.namespace, pod_name, secret_name)
            sys.exit(1)

        # Step 6: Execute post-hooks (best-effort)
        post_hooks = restore_config.get('postHooks', [])
        if post_hooks:
            print("Executing post-hooks...")
            api_client = v1.api_client
            result = execute_hooks(api_client, args.namespace, post_hooks, mode='post')
            if not result['success']:
                print(f"Warning: Some post-hooks failed: {result['failed']}", file=sys.stderr)
            else:
                print("Post-hooks completed successfully")

        # Step 7: Cleanup pod + secret
        _cleanup_restore_resources(v1, args.namespace, pod_name, secret_name)

        print(f"\n✅ Restore complete: archive '{args.archive_id}' → PVC '{target_pvc}'")

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(1)


def _cleanup_restore_resources(v1: client.CoreV1Api, namespace: str, pod_name: str, secret_name: str) -> None:
    """Delete restore pod and secret, ignore errors.

    Args:
        v1: Kubernetes CoreV1Api instance
        namespace: Namespace of the resources
        pod_name: Pod name to delete
        secret_name: Secret name to delete
    """
    try:
        v1.delete_namespaced_pod(pod_name, namespace)
        print(f"Cleaned up restore pod: {pod_name}")
    except Exception:
        pass  # Ignore cleanup errors

    try:
        v1.delete_namespaced_secret(secret_name, namespace)
        print(f"Cleaned up restore secret: {secret_name}")
    except Exception:
        pass  # Ignore cleanup errors
