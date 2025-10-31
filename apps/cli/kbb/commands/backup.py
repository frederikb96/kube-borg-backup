"""Borg backup restore commands."""

import argparse
import json
import signal
import sys
import time
from typing import Any
import yaml
from kubernetes import client
from kubernetes.client.exceptions import ApiException

from common.pod_monitor import PodMonitor
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

        # Extract image config from pod section (with fallback for backward compatibility)
        pod_config = config.get('pod', {})
        default_repo = 'ghcr.io/frederikb96/kube-borg-backup/backup-runner'
        image_repository = pod_config.get('image', {}).get('repository', default_repo)
        image_tag = pod_config.get('image', {}).get('tag', 'latest')
        image = f"{image_repository}:{image_tag}"

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
                        image=image,
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

        # Setup signal handling for graceful cleanup
        def handle_signal(signum, frame):
            """Handle termination signals - cleanup spawned resources."""
            print("\nStopping operation, cleaning up resources (up to 30s)...", file=sys.stderr, flush=True)
            cleanup_with_grace_period(v1, args.namespace, pod_name, secret_name)
            sys.exit(143)  # 128 + 15 (SIGTERM)

        # Register signal handlers
        old_sigterm = signal.signal(signal.SIGTERM, handle_signal)
        old_sigint = signal.signal(signal.SIGINT, handle_signal)
        old_sighup = signal.signal(signal.SIGHUP, handle_signal)

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

        # Restore original signal handlers
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGHUP, old_sighup)

        # Cleanup pod and secret
        cleanup_list_resources(v1, args.namespace, pod_name, secret_name)

        # Display results
        all_archives = archive_data.get('archives', [])
        repository = archive_data.get('repository', 'Unknown')

        # Extract archive prefixes from config (backups[].name contains the prefix)
        # Archive naming: {prefix}-{timestamp}
        # Prefix can be custom (archivePrefix) or default ({app-name}-{backup-name})
        backups = config.get('backups', [])
        archive_prefixes = [backup.get('name') for backup in backups if backup.get('name')]

        if not archive_prefixes:
            print("Error: No backup configurations found in config", file=sys.stderr)
            sys.exit(1)

        # Filter archives that match any of the configured prefixes
        archives = [
            a for a in all_archives
            if any(a.get('name', '').startswith(f"{prefix}-") for prefix in archive_prefixes)
        ]

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


def cleanup_with_grace_period(v1: client.CoreV1Api, namespace: str, pod_name: str, secret_name: str) -> None:
    """Cleanup resources with 30s grace period, force delete if needed.

    Polls for pod deletion (404 from API) which is the only reliable signal
    that Kubernetes has fully removed the pod object.

    Args:
        v1: CoreV1Api client
        namespace: Kubernetes namespace
        pod_name: Pod name to delete
        secret_name: Secret name to delete
    """
    # Delete pod with default grace period (30s in Kubernetes)
    try:
        print(f"Deleting pod '{pod_name}'...", file=sys.stderr, flush=True)
        v1.delete_namespaced_pod(pod_name, namespace)
    except Exception as e:
        print(f"Warning: Failed to delete pod: {e}", file=sys.stderr, flush=True)

    # Wait up to 30 seconds for pod to terminate (poll for 404)
    start_time = time.time()
    pod_terminated = False

    while time.time() - start_time < 30:
        try:
            v1.read_namespaced_pod_status(pod_name, namespace)
            # Pod still exists in API - keep waiting
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # Pod fully deleted from API - SUCCESS
                pod_terminated = True
                elapsed = int(time.time() - start_time)
                print(f"Pod terminated gracefully after {elapsed}s", file=sys.stderr, flush=True)
                break
            # Other API error - log and retry
            print(f"Warning: API error checking pod status: {e}", file=sys.stderr, flush=True)

        time.sleep(1)

    # Force delete if pod didn't terminate
    if not pod_terminated:
        try:
            print("Warning: Pod did not terminate after 30s, force deleting...", file=sys.stderr, flush=True)
            v1.delete_namespaced_pod(
                pod_name,
                namespace,
                grace_period_seconds=0,
                propagation_policy='Background'
            )
            print(f"Warning: Pod force deleted. Check for stale resources in namespace {namespace}",
                  file=sys.stderr, flush=True)
        except Exception as e:
            print(f"Warning: Force delete failed: {e}", file=sys.stderr, flush=True)

    # Delete secret
    try:
        v1.delete_namespaced_secret(secret_name, namespace)
        print("Secret deleted", file=sys.stderr, flush=True)
    except Exception:
        pass  # Ignore cleanup errors


def restore_borg_archive(args: argparse.Namespace) -> None:
    """Restore from borg archive with FUSE mount.

    Full workflow:
    1. Load config and restore hooks
    2. Execute pre-hooks (fail-fast)
    3. Determine target PVC (explicit or first backup PVC)
    4. Create ephemeral config Secret
    5. Spawn borg-restore pod (waits indefinitely for completion)
    6. Execute post-hooks (ONLY on success! Skip on failure to avoid scaling up broken deployment)
    7. Cleanup pod + secret (ALWAYS, even on failure)

    Args:
        args: CLI arguments with namespace, app, release, archive_id, optional pvc
    """
    from common.hooks import execute_hooks

    # Track resources for cleanup
    v1 = None
    pod_name = None
    secret_name = None
    restore_succeeded = False
    restore_config = {}
    target_pvc = None

    try:
        # Step 1: Load config
        config = find_app_config(args.namespace, args.app, args.release, config_type='borg')
        restore_config = config.get('restore', {})

        # Extract image config from pod section (with fallback for backward compatibility)
        pod_config = config.get('pod', {})
        default_repo = 'ghcr.io/frederikb96/kube-borg-backup/backup-runner'
        image_repository = pod_config.get('image', {}).get('repository', default_repo)
        image_tag = pod_config.get('image', {}).get('tag', 'latest')
        image = f"{image_repository}:{image_tag}"

        # Step 2: Execute pre-hooks (fail-fast)
        pre_hooks = restore_config.get('preHooks', [])
        if pre_hooks:
            print("Executing pre-hooks...")
            v1, _ = load_kube_client()
            api_client = v1.api_client
            result = execute_hooks(api_client, args.namespace, pre_hooks, mode='pre')
            if not result['success']:
                print(f"❌ Pre-hooks failed: {result['failed']}", file=sys.stderr)
                sys.exit(1)
            print("✅ Pre-hooks completed successfully")

        # Step 3: Determine target PVC
        if not v1:
            v1, _ = load_kube_client()

        if args.pvc:
            target_pvc = args.pvc
        else:
            # Auto-detect from archive name
            # Archive format: {prefix}-YYYY-MM-DD-HH-MM-SS
            # Check if archive starts with any backup name from config
            backups = config.get('backups', [])
            if not backups:
                print("Error: No backups configured in config", file=sys.stderr)
                sys.exit(1)

            # Find matching backup: archive must start with "{backup_name}-"
            matching_backups = [
                b for b in backups
                if args.archive_id.startswith(b.get('name', '') + '-')
            ]

            if not matching_backups:
                print(f"Error: Archive '{args.archive_id}' doesn't match any configured backup", file=sys.stderr)
                print(f"Available backups: {', '.join(b.get('name', 'N/A') for b in backups)}", file=sys.stderr)
                print("Specify target PVC manually with --pvc flag", file=sys.stderr)
                sys.exit(1)

            backup_name = matching_backups[0]['name']
            target_pvc = matching_backups[0]['pvc']
            print(f"Auto-detected target PVC from backup '{backup_name}': {target_pvc}")

        # Verify target PVC exists
        try:
            v1.read_namespaced_persistent_volume_claim(target_pvc, args.namespace)
        except ApiException as e:
            if e.status == 404:
                print(f"Error: Target PVC '{target_pvc}' not found in namespace '{args.namespace}'", file=sys.stderr)
                sys.exit(1)
            print(f"Error checking PVC: {e}", file=sys.stderr)
            sys.exit(1)

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

        # Step 5: Spawn borg-restore pod (no timeout - can take hours for large datasets)
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
                        image=image,
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

        print(f"Spawning borg restore pod '{pod_name}'...")
        print("⏳ Restoring from borg archive...")

        v1.create_namespaced_pod(args.namespace, pod)
        print("Borg restore pod created")

        # Setup signal handling for graceful cleanup
        def handle_signal_restore(signum, frame):
            """Handle termination signals - cleanup spawned resources."""
            print("\nStopping restore, cleaning up resources (up to 30s)...", file=sys.stderr, flush=True)
            _cleanup_restore_with_grace_period(v1, args.namespace, pod_name, secret_name)
            sys.exit(143)  # 128 + 15 (SIGTERM)

        # Register signal handlers
        old_sigterm = signal.signal(signal.SIGTERM, handle_signal_restore)
        old_sigint = signal.signal(signal.SIGINT, handle_signal_restore)
        old_sighup = signal.signal(signal.SIGHUP, handle_signal_restore)

        # Start monitoring (events + logs in background threads)
        monitor = PodMonitor(v1, pod_name, args.namespace)
        monitor.start()

        # Monitor pod status (no timeout - wait indefinitely)
        while True:
            try:
                pod_status = v1.read_namespaced_pod_status(pod_name, args.namespace)
                phase = pod_status.status.phase

                if phase == 'Succeeded':
                    # Stop monitoring
                    monitor.stop()
                    print("✅ Restore completed successfully")
                    restore_succeeded = True
                    break
                elif phase == 'Failed':
                    # Stop monitoring
                    monitor.stop()

                    # Get logs for error context
                    try:
                        logs = v1.read_namespaced_pod_log(pod_name, args.namespace)
                        print(f"❌ Restore pod failed. Last logs:\n{logs}", file=sys.stderr)
                    except ApiException:
                        print("❌ Restore pod failed (could not retrieve logs)", file=sys.stderr)

                    restore_succeeded = False
                    break

            except ApiException as e:
                monitor.stop()
                print(f"⚠️  Error checking pod status: {e}", file=sys.stderr)
                restore_succeeded = False
                break

            time.sleep(5)

        # Restore original signal handlers
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGHUP, old_sighup)

    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        restore_succeeded = False
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        restore_succeeded = False

    finally:
        # Step 6: Execute post-hooks (ONLY on success!)
        if restore_succeeded:
            post_hooks = restore_config.get('postHooks', [])
            if post_hooks:
                print("Executing post-hooks...")
                try:
                    api_client = v1.api_client
                    result = execute_hooks(api_client, args.namespace, post_hooks, mode='post')
                    if not result['success']:
                        print(f"⚠️  Warning: Some post-hooks failed: {result['failed']}", file=sys.stderr)
                    else:
                        print("✅ Post-hooks completed successfully")
                except Exception as e:
                    print(f"⚠️  Warning: Post-hooks failed: {e}", file=sys.stderr)
        else:
            print("⚠️  Post-hooks NOT executed due to restore failure", file=sys.stderr)

        # Step 7: Cleanup pod + secret (ALWAYS!)
        if v1 and (pod_name or secret_name):
            try:
                _cleanup_restore_resources(v1, args.namespace, pod_name, secret_name)
            except Exception as e:
                print(f"⚠️  Warning: Cleanup failed: {e}", file=sys.stderr)

    # Exit with appropriate code
    if not restore_succeeded:
        sys.exit(1)

    print(f"\n✅ Restore complete: archive '{args.archive_id}' → PVC '{target_pvc}'")


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


def _cleanup_restore_with_grace_period(v1: client.CoreV1Api, namespace: str, pod_name: str, secret_name: str) -> None:
    """Cleanup restore resources with 30s grace period, force delete if needed.

    Polls for pod deletion (404 from API) which is the only reliable signal
    that Kubernetes has fully removed the pod object.

    Args:
        v1: CoreV1Api client
        namespace: Kubernetes namespace
        pod_name: Pod name to delete
        secret_name: Secret name to delete
    """
    # Delete pod with default grace period (30s in Kubernetes)
    try:
        print(f"Deleting restore pod '{pod_name}'...", file=sys.stderr, flush=True)
        v1.delete_namespaced_pod(pod_name, namespace)
    except Exception as e:
        print(f"Warning: Failed to delete pod: {e}", file=sys.stderr, flush=True)

    # Wait up to 30 seconds for pod to terminate (poll for 404)
    start_time = time.time()
    pod_terminated = False

    while time.time() - start_time < 30:
        try:
            v1.read_namespaced_pod_status(pod_name, namespace)
            # Pod still exists in API - keep waiting
        except client.exceptions.ApiException as e:
            if e.status == 404:
                # Pod fully deleted from API - SUCCESS
                pod_terminated = True
                elapsed = int(time.time() - start_time)
                print(f"Restore pod terminated gracefully after {elapsed}s", file=sys.stderr, flush=True)
                break
            # Other API error - log and retry
            print(f"Warning: API error checking pod status: {e}", file=sys.stderr, flush=True)

        time.sleep(1)

    # Force delete if pod didn't terminate
    if not pod_terminated:
        try:
            print("Warning: Restore pod did not terminate after 30s, force deleting...", file=sys.stderr, flush=True)
            v1.delete_namespaced_pod(
                pod_name,
                namespace,
                grace_period_seconds=0,
                propagation_policy='Background'
            )
            print(f"Warning: Restore pod force deleted. Check for stale resources in namespace {namespace}",
                  file=sys.stderr, flush=True)
        except Exception as e:
            print(f"Warning: Force delete failed: {e}", file=sys.stderr, flush=True)

    # Delete secret
    try:
        v1.delete_namespaced_secret(secret_name, namespace)
        print("Restore secret deleted", file=sys.stderr, flush=True)
    except Exception:
        pass  # Ignore cleanup errors
