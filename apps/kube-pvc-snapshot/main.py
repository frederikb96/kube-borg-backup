"""Create and prune PVC snapshots using the Kubernetes API.

The script expects a YAML config mounted at /config/config.yaml with the
following structure::

    namespace: default
    hooks:
      pre: |
        echo "pause"
      post: |
        echo "resume"
    snapshots:
      - pvc: my-pvc
        class: longhorn
        keep:
          n: 12
          m_hours: 24

It executes optional pre/post shell hooks, then creates a VolumeSnapshot for
each entry and prunes old snapshots according to the keep policy.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List

import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

GROUP = "snapshot.storage.k8s.io"
VERSION = "v1"
PLURAL = "volumesnapshots"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PVC snapshot helper")
    parser.add_argument("-c", "--config", help="Path to config file")
    return parser.parse_args()


def resolve_config_path(cli_path: str | None) -> Path:
    if cli_path:
        return Path(cli_path)
    env_path = os.getenv("APP_CONFIG")
    if env_path:
        return Path(env_path)
    return Path("/config/config.yaml")


def load_config(cli_path: str | None) -> Dict[str, Any]:
    path = resolve_config_path(cli_path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        print(f"Config file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Failed to read config {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("Config root must be a mapping", file=sys.stderr)
        sys.exit(2)
    return data


def init_client() -> client.CustomObjectsApi:
    try:
        config.load_incluster_config()
    except ConfigException:
        try:
            config.load_kube_config()
        except Exception as exc:
            print(f"Failed to load kubeconfig: {exc}", file=sys.stderr)
            sys.exit(3)
    return client.CustomObjectsApi()


def run_hook(script: str | None) -> None:
    if not script:
        return
    try:
        subprocess.run(["/bin/sh", "-c", script], check=True)
    except subprocess.CalledProcessError as exc:
        print(f"Hook failed with exit code {exc.returncode}", file=sys.stderr)
        sys.exit(exc.returncode)


def create_snapshot(api: client.CustomObjectsApi, pvc: str, cls: str, namespace: str) -> str:
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    name = f"{pvc}-snap-{ts}"
    body = {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "VolumeSnapshot",
        "metadata": {"name": name, "namespace": namespace, "labels": {"pvc": pvc}},
        "spec": {
            "volumeSnapshotClassName": cls,
            "source": {"persistentVolumeClaimName": pvc},
        },
    }
    api.create_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, body)
    return name


def wait_snapshot_ready(api: client.CustomObjectsApi, name: str, namespace: str, timeout: int = 20) -> None:
    end = time.time() + timeout
    while time.time() < end:
        snap = api.get_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
        if snap.get("status", {}).get("readyToUse"):
            return
        time.sleep(2)
    print(f"Snapshot {name} not ready after {timeout}s", file=sys.stderr)
    sys.exit(1)


def prune_snapshots(api: client.CustomObjectsApi, pvc: str, keep_n: int, keep_m_hours: int, namespace: str) -> None:
    snaps = api.list_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, label_selector=f"pvc={pvc}")
    items = snaps.get("items", [])
    items.sort(key=lambda s: s.get("metadata", {}).get("creationTimestamp", ""), reverse=True)
    preserve: List[str] = []
    preserve.extend(i.get("metadata", {}).get("name") for i in items[:keep_n])
    threshold = datetime.utcnow() - timedelta(hours=keep_m_hours)
    for s in items:
        ts = s.get("metadata", {}).get("creationTimestamp")
        try:
            created = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except ValueError:
            created = None
        if created and created >= threshold:
            preserve.append(s.get("metadata", {}).get("name"))
    preserve_set = set(filter(None, preserve))
    for s in items:
        name = s.get("metadata", {}).get("name")
        if name in preserve_set:
            continue
        try:
            api.delete_namespaced_custom_object(GROUP, VERSION, namespace, PLURAL, name)
            print(f"Deleted old snapshot {name}")
        except ApiException as exc:
            print(f"Failed to delete snapshot {name}: {exc}", file=sys.stderr)


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    namespace = cfg.get("namespace", "default")
    hooks = cfg.get("hooks", {}) if isinstance(cfg, dict) else {}
    api = init_client()
    run_hook(hooks.get("pre"))
    snaps_cfg = cfg.get("snapshots", [])
    for snap in snaps_cfg:
        pvc = snap.get("pvc")
        cls = snap.get("class")
        if not pvc or not cls:
            print("Snapshot entry missing pvc or class", file=sys.stderr)
            sys.exit(2)
        name = create_snapshot(api, pvc, cls, namespace)
        wait_snapshot_ready(api, name, namespace)
    for snap in snaps_cfg:
        pvc = snap.get("pvc")
        keep = snap.get("keep", {})
        n = int(keep.get("n", 0))
        m_hours = int(keep.get("m_hours", 0))
        prune_snapshots(api, pvc, n, m_hours, namespace)
    run_hook(hooks.get("post"))


if __name__ == "__main__":
    main()
