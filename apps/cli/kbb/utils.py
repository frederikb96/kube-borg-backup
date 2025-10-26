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


def find_app_config(namespace: str, app_name: str, release_name: str, config_type: str = 'snapshot') -> dict[str, Any]:
    """Find config Secret for app via direct name construction.

    Secret naming convention: {release_name}-{app_name}-{config_type}-config

    Args:
        namespace: Kubernetes namespace
        app_name: Application name
        release_name: Helm release name
        config_type: Config type ('snapshot' or 'borg')

    Returns:
        Parsed config dict from Secret's config.yaml

    Raises:
        ValueError: If config not found or invalid
    """
    v1, _ = load_kube_client()

    # Construct Secret name directly
    secret_name = f"{release_name}-{app_name}-{config_type}-config"

    try:
        secret = v1.read_namespaced_secret(secret_name, namespace)
    except ApiException as e:
        if e.status == 404:
            raise ValueError(
                f"Config Secret not found: '{secret_name}' in namespace '{namespace}'\n"
                f"Expected Secret from Helm release '{release_name}' for app '{app_name}'"
            ) from e
        raise ValueError(f"Failed to read Secret '{secret_name}': {e}") from e

    # Parse config.yaml from Secret
    config_data_b64 = secret.data.get('config.yaml')
    if not config_data_b64:
        raise ValueError(f"Secret '{secret_name}' missing config.yaml data")

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
