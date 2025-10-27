"""Helper functions for restore operations."""

import sys
import time
import threading
from typing import Any
from kubernetes import client
from kubernetes.client.exceptions import ApiException
from kubernetes import watch
from kbb.utils import load_kube_client


def create_clone_pvc(
    namespace: str,
    snapshot_name: str,
    clone_pvc_name: str,
    storage_class: str,
    size: str | None = None
) -> dict[str, Any]:
    """Create clone PVC from VolumeSnapshot.

    Args:
        namespace: Kubernetes namespace
        snapshot_name: Source VolumeSnapshot name
        clone_pvc_name: Name for new clone PVC
        storage_class: Storage class for clone PVC
        size: Optional size override (defaults to snapshot size)

    Returns:
        Dict with:
            - name: Clone PVC name
            - binding_mode: Storage class binding mode ('Immediate' or 'WaitForFirstConsumer')
            - status: Initial PVC status

    Raises:
        Exception: If PVC creation fails
    """
    v1, custom_api = load_kube_client()

    # Get storage class to check binding mode
    try:
        storage_v1 = client.StorageV1Api()
        sc = storage_v1.read_storage_class(storage_class)
        binding_mode = sc.volume_binding_mode  # 'Immediate' or 'WaitForFirstConsumer'
    except client.exceptions.ApiException as e:
        print(f"Error reading storage class '{storage_class}': {e}", file=sys.stderr, flush=True)
        raise

    # Get snapshot to extract size if not provided
    if not size:
        try:
            snapshot = custom_api.get_namespaced_custom_object(
                group="snapshot.storage.k8s.io",
                version="v1",
                namespace=namespace,
                plural="volumesnapshots",
                name=snapshot_name
            )
        except client.exceptions.ApiException as e:
            print(f"Error reading VolumeSnapshot '{snapshot_name}': {e}", file=sys.stderr, flush=True)
            raise

        # Extract size from snapshot status
        size = snapshot.get('status', {}).get('restoreSize')
        if not size:
            raise ValueError(
                f"Could not determine size from VolumeSnapshot '{snapshot_name}'. "
                f"Please provide size parameter explicitly."
            )

    # Create PVC with snapshot as dataSource
    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=clone_pvc_name, namespace=namespace),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=client.V1ResourceRequirements(
                requests={"storage": size}
            ),
            storage_class_name=storage_class,
            data_source=client.V1TypedLocalObjectReference(
                api_group="snapshot.storage.k8s.io",
                kind="VolumeSnapshot",
                name=snapshot_name
            )
        )
    )

    try:
        created_pvc = v1.create_namespaced_persistent_volume_claim(namespace, pvc)
    except client.exceptions.ApiException as e:
        print(f"Error creating clone PVC '{clone_pvc_name}': {e}", file=sys.stderr, flush=True)
        raise

    return {
        "name": clone_pvc_name,
        "binding_mode": binding_mode,
        "status": created_pvc.status.phase
    }


def spawn_rsync_pod(
    namespace: str,
    source_pvc_name: str,
    target_pvc_name: str,
    pod_name: str | None = None,
    image_repository: str = 'alpine',
    image_tag: str = 'latest'
) -> dict[str, Any]:
    """Spawn rsync pod to copy data from source PVC to target PVC.

    Creates an ephemeral Alpine pod that:
    1. Installs rsync
    2. Mounts source PVC read-only at /source
    3. Mounts target PVC read-write at /target
    4. Runs rsync --delete to sync data
    5. Self-terminates on completion

    Waits indefinitely for pod completion (no timeout).
    Streams logs in real-time using Kubernetes watch API.

    Args:
        namespace: Kubernetes namespace
        source_pvc_name: Source PVC name (clone from snapshot)
        target_pvc_name: Target PVC name (destination)
        pod_name: Optional pod name (auto-generated if not provided)
        image_repository: Container image repository (default: alpine)
        image_tag: Container image tag (default: latest)

    Returns:
        Dict with:
            - success: bool
            - pod_name: Pod name used

    Raises:
        Exception: If pod fails
    """
    v1, _ = load_kube_client()

    # Generate pod name if not provided
    if not pod_name:
        pod_name = f"rsync-{int(time.time())}"

    # Create pod spec with privileged mode to bypass filesystem permissions
    # (clone PVCs may have restrictive ownership like postgres 70:70 with mode 0700)
    pod = client.V1Pod(
        metadata=client.V1ObjectMeta(
            name=pod_name,
            namespace=namespace,
            labels={"app": "kube-borg-backup", "operation": "rsync"}
        ),
        spec=client.V1PodSpec(
            containers=[
                client.V1Container(
                    name="rsync",
                    image=f"{image_repository}:{image_tag}",
                    command=["/bin/sh", "-c"],
                    args=["apk add --no-cache rsync && rsync -av --delete /source/ /target/"],
                    volume_mounts=[
                        client.V1VolumeMount(name="source", mount_path="/source", read_only=True),
                        client.V1VolumeMount(name="target", mount_path="/target")
                    ],
                    security_context=client.V1SecurityContext(privileged=True)
                )
            ],
            volumes=[
                client.V1Volume(
                    name="source",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=source_pvc_name,
                        read_only=True
                    )
                ),
                client.V1Volume(
                    name="target",
                    persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                        claim_name=target_pvc_name
                    )
                )
            ],
            restart_policy="Never"
        )
    )

    # Create pod
    try:
        v1.create_namespaced_pod(namespace, pod)
        print(f"Rsync pod '{pod_name}' created", flush=True)
    except ApiException as e:
        print(f"Error creating rsync pod '{pod_name}': {e}", file=sys.stderr, flush=True)
        raise

    print(f"⏳ Waiting for rsync pod to complete...", flush=True)

    # Start log streaming in background thread
    stop_event = threading.Event()
    log_thread = threading.Thread(
        target=_stream_pod_logs,
        args=(v1, pod_name, namespace, stop_event),
        daemon=True
    )
    log_thread.start()

    # Monitor pod status (no timeout - wait indefinitely)
    while True:
        try:
            pod_status = v1.read_namespaced_pod_status(pod_name, namespace)
            phase = pod_status.status.phase

            if phase == "Succeeded":
                # Stop log streaming
                stop_event.set()
                log_thread.join(timeout=5)

                print(f"✅ Rsync pod completed successfully", flush=True)

                # Cleanup pod
                try:
                    v1.delete_namespaced_pod(pod_name, namespace)
                except ApiException:
                    pass  # Ignore deletion errors

                return {"success": True, "pod_name": pod_name}

            elif phase == "Failed":
                # Stop log streaming
                stop_event.set()
                log_thread.join(timeout=5)

                # Get logs for error context
                try:
                    logs = v1.read_namespaced_pod_log(pod_name, namespace)
                except ApiException:
                    logs = "Could not retrieve pod logs"

                # Cleanup pod
                try:
                    v1.delete_namespaced_pod(pod_name, namespace)
                except ApiException:
                    pass  # Ignore deletion errors

                raise Exception(f"Rsync pod '{pod_name}' failed:\n{logs}")

        except ApiException as e:
            stop_event.set()
            print(f"⚠️  Error checking pod status: {e}", file=sys.stderr, flush=True)
            raise

        time.sleep(5)


def _stream_pod_logs(
    v1: client.CoreV1Api,
    pod_name: str,
    namespace: str,
    stop_event: threading.Event
) -> None:
    """Stream pod logs to stdout in real-time (runs in background thread).

    Args:
        v1: CoreV1Api client
        pod_name: Pod name to stream logs from
        namespace: Kubernetes namespace
        stop_event: Threading event to signal when to stop streaming
    """
    try:
        # Wait for container to be ready
        while not stop_event.is_set():
            try:
                pod = v1.read_namespaced_pod(pod_name, namespace)

                # Check container status
                if pod.status.container_statuses:
                    for container in pod.status.container_statuses:
                        # Container running - ready to stream
                        if container.state.running and container.state.running.started_at:
                            break

                        # Container terminated - need fallback
                        if container.state.terminated:
                            break
                    else:
                        # No container ready yet, keep polling
                        time.sleep(2)
                        continue

                    # Found container in ready state, break outer loop
                    break
            except ApiException:
                pass

            time.sleep(2)

        # If stop_event was set before container ready, exit
        if stop_event.is_set():
            return

        # Try streaming logs with follow=True
        try:
            log_stream = v1.read_namespaced_pod_log(
                pod_name,
                namespace,
                follow=True,
                _preload_content=False
            )

            # Stream logs line by line
            for line in log_stream:
                if stop_event.is_set():
                    break
                line_str = line.decode('utf-8').rstrip('\n\r')
                if line_str:
                    print(f"[{pod_name}] {line_str}", flush=True)

        except ApiException as exc:
            # Handle "Bad Request" - pod completed before streaming started
            if hasattr(exc, 'status') and exc.status == 400:
                # Fallback: Read all logs without follow
                try:
                    logs = v1.read_namespaced_pod_log(pod_name, namespace)
                    if logs:
                        for line in logs.split('\n'):
                            if line.strip():
                                print(f"[{pod_name}] {line}", flush=True)
                except ApiException:
                    pass
            elif not stop_event.is_set():
                # Other error - log it
                reason = exc.reason if hasattr(exc, 'reason') else str(exc)
                print(f"⚠️  Log streaming ended: {reason}", file=sys.stderr, flush=True)

    except Exception as exc:
        if not stop_event.is_set():
            print(f"⚠️  Error streaming logs: {exc}", file=sys.stderr, flush=True)
