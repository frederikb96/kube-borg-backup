"""Restore snapshots to temporary PVCs and run borg backups."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

import yaml
from jinja2 import Environment, FileSystemLoader
from kubernetes import client, config
from kubernetes.client.rest import ApiException
from kubernetes.config.config_exception import ConfigException

SNAP_GROUP = "snapshot.storage.k8s.io"
SNAP_VERSION = "v1"
SNAP_PLURAL = "volumesnapshots"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run borg backups from PVC snapshots")
    parser.add_argument("-c", "--config", help="Path to config file")
    return parser.parse_args()


def resolve_config_path(cli: str | None) -> Path:
    if cli:
        return Path(cli)
    env = os.getenv("APP_CONFIG")
    if env:
        return Path(env)
    return Path("/config/config.yaml")


def load_config(path: str | None) -> Dict[str, Any]:
    cfg_path = resolve_config_path(path)
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except FileNotFoundError:
        print(f"Config file not found: {cfg_path}", file=sys.stderr)
        sys.exit(2)
    except Exception as exc:
        print(f"Failed to read config {cfg_path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print("Config root must be a mapping", file=sys.stderr)
        sys.exit(2)
    return data


def init_clients() -> tuple[client.CoreV1Api, client.CustomObjectsApi]:
    try:
        config.load_incluster_config()
    except ConfigException:
        try:
            config.load_kube_config()
        except Exception as exc:
            print(f"Failed to load kubeconfig: {exc}", file=sys.stderr)
            sys.exit(3)
    return client.CoreV1Api(), client.CustomObjectsApi()


def latest_snapshot(snap_api: client.CustomObjectsApi, pvc: str, namespace: str) -> str:
    snaps = snap_api.list_namespaced_custom_object(SNAP_GROUP, SNAP_VERSION, namespace, SNAP_PLURAL, label_selector=f"pvc={pvc}")
    items = [s for s in snaps.get("items", []) if s.get("status", {}).get("readyToUse")]
    items.sort(key=lambda s: s.get("metadata", {}).get("creationTimestamp", ""))
    if not items:
        print(f"No ready snapshot found for {pvc}", file=sys.stderr)
        sys.exit(1)
    return items[-1]["metadata"]["name"]


def create_clone(v1: client.CoreV1Api, snap_api: client.CustomObjectsApi, snap_name: str, clone_name: str, storage_class: str, namespace: str) -> None:
    snap = snap_api.get_namespaced_custom_object(SNAP_GROUP, SNAP_VERSION, namespace, SNAP_PLURAL, snap_name)
    size = snap.get("status", {}).get("restoreSize", "1Gi")
    body = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": clone_name, "namespace": namespace, "labels": {"pvc": f"{clone_name}"}},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}},
            "dataSource": {
                "name": snap_name,
                "kind": "VolumeSnapshot",
                "apiGroup": SNAP_GROUP,
            },
        },
    }
    v1.create_namespaced_persistent_volume_claim(namespace, body)


def wait_pvc_bound(v1: client.CoreV1Api, name: str, namespace: str, timeout: int = 300) -> None:
    end = time.time() + timeout
    while time.time() < end:
        pvc = v1.read_namespaced_persistent_volume_claim(name, namespace)
        if pvc.status.phase == "Bound":
            return
        time.sleep(5)
    print(f"PVC {name} not bound after {timeout}s", file=sys.stderr)
    sys.exit(1)


def render_pod_template(path: Path, context: Dict[str, Any]) -> Dict[str, Any]:
    env = Environment(loader=FileSystemLoader(str(path.parent)))
    template = env.get_template(path.name)
    rendered = template.render(**context)
    data = yaml.safe_load(rendered)
    if not isinstance(data, dict):
        print("Rendered pod template must be a mapping", file=sys.stderr)
        sys.exit(2)
    return data


def run_pod(v1: client.CoreV1Api, manifest: Dict[str, Any], namespace: str, timeout: int) -> None:
    name = manifest["metadata"]["name"]
    v1.create_namespaced_pod(namespace, manifest)
    end = time.time() + timeout
    while time.time() < end:
        pod = v1.read_namespaced_pod(name, namespace)
        phase = pod.status.phase
        if phase in {"Succeeded", "Failed"}:
            logs = v1.read_namespaced_pod_log(name, namespace)
            if logs:
                print(logs)
            if phase != "Succeeded":
                print(f"Pod {name} failed", file=sys.stderr)
                raise RuntimeError("pod failed")
            return
        time.sleep(10)
    raise RuntimeError(f"Pod {name} timeout after {timeout}s")


def delete_pod(v1: client.CoreV1Api, name: str, namespace: str) -> None:
    try:
        v1.delete_namespaced_pod(name, namespace)
    except ApiException:
        pass


def delete_pvc(v1: client.CoreV1Api, name: str, namespace: str) -> None:
    try:
        v1.delete_namespaced_persistent_volume_claim(name, namespace)
    except ApiException:
        pass


def main() -> None:
    args = parse_args()
    cfg = load_config(args.config)
    namespace = cfg.get("namespace", "default")
    pod_cfg = cfg.get("pod", {})
    backups = cfg.get("backups", [])
    v1, snap_api = init_clients()

    pod_template = Path(os.getenv("POD_TEMPLATE", "/app/pod.yaml.j2"))

    for item in backups:
        name = item.get("name")
        pvc = item.get("pvc")
        cls = item.get("class")
        if not (name and pvc and cls):
            print("Backup entry missing name, pvc or class", file=sys.stderr)
            sys.exit(2)
        snap = latest_snapshot(snap_api, pvc, namespace)
        clone = f"{snap}-clone"
        create_clone(v1, snap_api, snap, clone, cls, namespace)
        wait_pvc_bound(v1, clone, namespace)
        context = {
            "pod_name": pod_cfg.get("name", "borg"),
            "namespace": namespace,
            "image": pod_cfg.get("image"),
            "repo_secret": pod_cfg.get("repoSecret"),
            "ssh_secret": pod_cfg.get("sshSecret"),
            "cache_pvc": pod_cfg.get("cachePVC"),
            "borg_flags": pod_cfg.get("borgFlags", ""),
            "backup_name": name,
            "clone_pvc": clone,
        }
        manifest = render_pod_template(pod_template, context)
        try:
            run_pod(v1, manifest, namespace, int(pod_cfg.get("timeoutSeconds", 3600)))
        finally:
            delete_pod(v1, context["pod_name"], namespace)
            delete_pvc(v1, clone, namespace)


if __name__ == "__main__":
    main()
