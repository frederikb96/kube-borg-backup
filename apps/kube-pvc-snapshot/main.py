"""Minimal, three-phase Kubernetes demo.

This module is intentionally split into small, single-purpose functions so
each piece can be tested or reused independently:

- Configuration: Resolve a config path and load YAML (namespace + pod).
- Kubernetes init: Initialize a CoreV1 client (in-cluster, then local).
- Execution: List pods in a namespace and print their names.

Docstrings follow PEP 257 (the docstring convention referenced by PEP 8):
short summary on the first line, then optional details.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict

import yaml
from jinja2 import Environment, FileSystemLoader
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException


def parse_args() -> argparse.Namespace:
    """Parse command-line flags.

    Currently supports only ``--config`` (``-c``) to provide a path to the YAML
    configuration file. If omitted, the resolver will check ``APP_CONFIG`` and
    finally ``/config/config.yaml``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Load config and fetch a pod using the Kubernetes API."
        )
    )
    parser.add_argument(
        "-c",
        "--config",
        help=(
            "Path to YAML config file. Overrides APP_CONFIG and default "
            "/config/config.yaml"
        ),
    )
    return parser.parse_args()


def resolve_config_path(cli_path: str | None) -> Path:
    """Determine which config file to use.

    Precedence: CLI path > ``APP_CONFIG`` env var > ``/config/config.yaml``.
    """
    if cli_path:
        return Path(cli_path)
    env_path = os.getenv("APP_CONFIG")
    if env_path:
        return Path(env_path)
    return Path("/config/config.yaml")


def load_config_from_sources(cli_path: str | None) -> Dict[str, Any]:
    """Load YAML configuration without enforcing schema.

    Exits with a non-zero status if the file is missing, unreadable, or the
    root is not a mapping. Each consumer function validates its own needs.
    """
    path = resolve_config_path(cli_path)

    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        print(f"Config file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Failed to read/parse config '{path}': {exc}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(data, dict):
        print("Config root must be a mapping (YAML object)", file=sys.stderr)
        sys.exit(2)

    return data


def init_kube_client() -> client.CoreV1Api:
    """Initialize a Kubernetes CoreV1 API client.

    Attempts in-cluster configuration first; falls back to local kubeconfig.
    Exits with a non-zero status if neither can be loaded.
    """
    try:
        config.load_incluster_config()
    except ConfigException:
        try:
            config.load_kube_config()
        except Exception as exc:
            print(f"Failed to load kubeconfig: {exc}", file=sys.stderr)
            sys.exit(3)
    return client.CoreV1Api()


def list_pods_in_namespace(api: client.CoreV1Api, cfg: Dict[str, Any]) -> None:
    """List pods in the namespace specified under ``read.namespace``.

    Prints each pod name on its own line. Exits with a non-zero status on
    validation or API errors.
    """
    read_cfg = cfg.get("read", {}) if isinstance(cfg, dict) else {}
    namespace = read_cfg.get("namespace")
    if not namespace:
        print("Missing read.namespace in config", file=sys.stderr)
        sys.exit(2)
    try:
        pods = api.list_namespaced_pod(namespace=namespace)
    except ApiException as exc:
        print(
            f"Kubernetes API error listing pods in {namespace}: {exc}",
            file=sys.stderr,
        )
        sys.exit(4)
    except Exception as exc:
        print(
            f"Unexpected error listing pods in {namespace}: {exc}",
            file=sys.stderr,
        )
        sys.exit(4)

    for item in pods.items or []:
        print(item.metadata.name)


def create_configmap_from_template(api: client.CoreV1Api, cfg: Dict[str, Any]) -> None:
    """Render a ConfigMap manifest from a template and apply it.

    Validates required keys in cfg["configmap"], delegates rendering to
    ``render_yaml_template``, and delegates API operations to
    ``apply_configmap``.
    """
    cm_cfg = cfg.get("configmap", {}) if isinstance(cfg, dict) else {}
    namespace = cm_cfg.get("namespace")
    name = cm_cfg.get("name")
    path = cm_cfg.get("path")
    content = cm_cfg.get("content", "")

    if not namespace or not name or not path:
        print(
            "Missing configmap.namespace, configmap.name, or configmap.path",
            file=sys.stderr,
        )
        sys.exit(2)

    manifest = render_yaml_template(path, {"name": name, "namespace": namespace, "content": content})
    apply_configmap(api, manifest)


def render_yaml_template(path: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """Render a Jinja2 template file and parse YAML into a dict.

    Exits with a non-zero status if the template is missing or the rendered
    output is not valid YAML mapping.
    """
    template_path = Path(path)
    if not template_path.exists():
        print(f"Template not found: {template_path}", file=sys.stderr)
        sys.exit(2)

    env = Environment(loader=FileSystemLoader(str(template_path.parent)))
    template = env.get_template(template_path.name)
    rendered = template.render(**context)

    try:
        data = yaml.safe_load(rendered)
    except Exception as exc:
        print(f"Rendered template is not valid YAML: {exc}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(data, dict):
        print("Rendered template root must be a mapping", file=sys.stderr)
        sys.exit(2)

    return data


def apply_configmap(api: client.CoreV1Api, manifest: Dict[str, Any]) -> None:
    """Create or replace a ConfigMap from a manifest dict.

    Requires metadata.name and metadata.namespace to be present.
    """
    meta = (manifest or {}).get("metadata", {})
    name = meta.get("name")
    namespace = meta.get("namespace")

    if not name or not namespace:
        print("ConfigMap manifest missing metadata.name or metadata.namespace", file=sys.stderr)
        sys.exit(2)

    try:
        api.create_namespaced_config_map(namespace=namespace, body=manifest)
        print(f"Created ConfigMap {namespace}/{name}")
    except ApiException as exc:
        if exc.status == 409:  # Already exists -> replace
            try:
                api.replace_namespaced_config_map(name=name, namespace=namespace, body=manifest)
                print(f"Replaced ConfigMap {namespace}/{name}")
            except ApiException as inner:
                print(
                    f"Failed to replace ConfigMap {namespace}/{name}: {inner}",
                    file=sys.stderr,
                )
                sys.exit(5)
        else:
            print(
                f"Failed to create ConfigMap {namespace}/{name}: {exc}",
                file=sys.stderr,
            )
            sys.exit(5)


def main() -> None:
    """Program entry point: parse, load, init, execute."""
    args = parse_args()
    cfg = load_config_from_sources(args.config)
    api = init_kube_client()
    list_pods_in_namespace(api, cfg)
    create_configmap_from_template(api, cfg)


if __name__ == "__main__":
    main()
