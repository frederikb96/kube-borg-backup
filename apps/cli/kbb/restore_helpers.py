"""Helper functions for restore operations."""

import sys
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
