"""Helper functions for restore operations."""

import sys
import time
from typing import Any
from kubernetes import client
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
        print(f"Error reading storage class '{storage_class}': {e}", file=sys.stderr)
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
            print(f"Error reading VolumeSnapshot '{snapshot_name}': {e}", file=sys.stderr)
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
        print(f"Error creating clone PVC '{clone_pvc_name}': {e}", file=sys.stderr)
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
    timeout: int | None = None,
    image_repository: str = 'ghcr.io/frederikb96/kube-borg-backup/backup-runner',
    image_tag: str = 'latest'
) -> dict[str, Any]:
    """Spawn rsync pod to copy data from source PVC to target PVC.

    Creates an ephemeral backup-runner pod that:
    1. Mounts source PVC read-only at /source
    2. Mounts target PVC read-write at /target
    3. Runs rsync --delete to sync data
    4. Self-terminates on completion

    Args:
        namespace: Kubernetes namespace
        source_pvc_name: Source PVC name (clone from snapshot)
        target_pvc_name: Target PVC name (destination)
        pod_name: Optional pod name (auto-generated if not provided)
        timeout: Optional timeout in seconds (None = no timeout, waits indefinitely)
                 WARNING: Setting timeout on restore can kill large data transfers!
        image_repository: Container image repository (default: backup-runner)
        image_tag: Container image tag (default: latest)

    Returns:
        Dict with:
            - success: bool
            - pod_name: Pod name used
            - logs: Pod logs (for debugging)

    Raises:
        Exception: If pod fails or times out
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
                    args=["rsync -av --delete /source/ /target/"],
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
        print(f"Rsync pod '{pod_name}' created in namespace '{namespace}'")
    except client.exceptions.ApiException as e:
        print(f"Error creating rsync pod '{pod_name}': {e}", file=sys.stderr)
        raise

    # Wait for completion (with optional timeout)
    start_time = time.time()
    while True:
        try:
            pod_status = v1.read_namespaced_pod_status(pod_name, namespace)
            phase = pod_status.status.phase

            if phase == "Succeeded":
                # Get logs before cleanup
                logs = v1.read_namespaced_pod_log(pod_name, namespace)
                print(f"Rsync pod '{pod_name}' completed successfully")

                # Cleanup pod
                try:
                    v1.delete_namespaced_pod(pod_name, namespace)
                    print(f"Rsync pod '{pod_name}' deleted")
                except client.exceptions.ApiException:
                    pass  # Ignore deletion errors

                return {"success": True, "pod_name": pod_name, "logs": logs}

            elif phase == "Failed":
                # Get logs before cleanup
                try:
                    logs = v1.read_namespaced_pod_log(pod_name, namespace)
                except client.exceptions.ApiException:
                    logs = "Could not retrieve pod logs"

                # Cleanup pod
                try:
                    v1.delete_namespaced_pod(pod_name, namespace)
                except client.exceptions.ApiException:
                    pass  # Ignore deletion errors

                raise Exception(f"Rsync pod '{pod_name}' failed:\n{logs}")

            # Only check timeout if one was provided
            if timeout is not None and (time.time() - start_time) > timeout:
                # Timeout - get logs and cleanup
                try:
                    logs = v1.read_namespaced_pod_log(pod_name, namespace)
                except client.exceptions.ApiException:
                    logs = "Could not retrieve pod logs"

                try:
                    v1.delete_namespaced_pod(pod_name, namespace)
                except client.exceptions.ApiException:
                    pass  # Ignore deletion errors

                raise Exception(f"Rsync pod '{pod_name}' timeout after {timeout}s:\n{logs}")

        except client.exceptions.ApiException as e:
            print(f"Error checking pod status: {e}", file=sys.stderr)

        time.sleep(2)
