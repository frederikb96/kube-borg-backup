"""Utility functions for k8s client and config discovery."""

from typing import Any
import base64
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException


def load_kube_client() -> tuple[client.CoreV1Api, client.CustomObjectsApi]:
    """Load kubeconfig and return API clients.

    Returns:
        Tuple of (CoreV1Api, CustomObjectsApi)
    """
    try:
        # Try loading from kubeconfig (local mode)
        config.load_kube_config()
    except config.ConfigException:
        # Fallback to in-cluster config (future feature)
        config.load_incluster_config()

    return client.CoreV1Api(), client.CustomObjectsApi()


def find_app_config(namespace: str, app_name: str) -> dict[str, Any]:
    """Find config Secret for app via labels.

    Searches for Secret with:
    - app.kubernetes.io/managed-by=kube-borg-backup
    - app.kubernetes.io/component=<app_name>

    Args:
        namespace: Kubernetes namespace
        app_name: Application name

    Returns:
        Parsed config dict from Secret's config.yaml

    Raises:
        ValueError: If config not found or invalid
    """
    v1, _ = load_kube_client()

    # List secrets with label selector
    label_selector = (
        'app.kubernetes.io/managed-by=kube-borg-backup,'
        f'app.kubernetes.io/component={app_name}'
    )

    try:
        secrets = v1.list_namespaced_secret(
            namespace,
            label_selector=label_selector
        )
    except ApiException as e:
        raise ValueError(f"Failed to list secrets in namespace '{namespace}': {e}")

    if not secrets.items:
        raise ValueError(
            f"No config found for app '{app_name}' in namespace '{namespace}'\n"
            f"Expected Secret with labels: {label_selector}"
        )

    # Parse config.yaml from Secret
    secret = secrets.items[0]
    config_data_b64 = secret.data.get('config.yaml')
    if not config_data_b64:
        raise ValueError(f"Secret '{secret.metadata.name}' missing config.yaml data")

    config_yaml = base64.b64decode(config_data_b64).decode('utf-8')
    config_data = yaml.safe_load(config_yaml)

    return config_data


def get_restore_hooks(config_data: dict[str, Any]) -> dict[str, Any]:
    """Extract restore hooks from config.

    Args:
        config_data: Parsed config dict

    Returns:
        Restore section dict with preHooks/postHooks
    """
    return config_data.get('restore', {})
