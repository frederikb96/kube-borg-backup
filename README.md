# kube-borg-backup

[![Release](https://img.shields.io/github/v/release/frederikb96/kube-borg-backup)](https://github.com/frederikb96/kube-borg-backup/releases)
[![License](https://img.shields.io/github/license/frederikb96/kube-borg-backup)](LICENSE)
[![Controller Build](https://img.shields.io/github/actions/workflow/status/frederikb96/kube-borg-backup/controller-image.yaml?label=controller)](https://github.com/frederikb96/kube-borg-backup/actions/workflows/controller-image.yaml)
[![Essentials Build](https://img.shields.io/github/actions/workflow/status/frederikb96/kube-borg-backup/essentials-image.yaml?label=essentials)](https://github.com/frederikb96/kube-borg-backup/actions/workflows/essentials-image.yaml)
[![Helm Chart](https://img.shields.io/badge/helm-v2.0.0-blue)](https://frederikb96.github.io/kube-borg-backup)

Kubernetes backup solution combining **instant CSI snapshots** with **async BorgBackup** for production-grade application backups.

## Why kube-borg-backup?

Get **instant, crash-consistent snapshots** of your entire Kubernetes application (databases + data), then **asynchronously backup** to offsite BorgBackup storage. Perfect for applications like Immich, Nextcloud, or any stateful workload where you need:

- **Instant point-in-time recovery** via CSI snapshots (seconds)
- **Offsite backup** to BorgBackup repository (disaster recovery)
- **Database consistency** via pre/post hooks (PostgreSQL, MySQL, etc.)
- **One Helm install per application** - complete backup solution

Developed specifically to solve the problem of backing up complex Kubernetes applications with multiple PVCs and databases while maintaining consistency and enabling fast recovery.

## Prerequisites

### Required

- **Kubernetes 1.25+**
- **CSI driver with VolumeSnapshot support**
  - For snapshot functionality
  - Examples: Longhorn, Ceph RBD, ZFS, AWS EBS CSI, etc.
  - Storage class must support CSI snapshots (creates `VolumeSnapshot` resources)
- **Storage class with snapshot cloning support**
  - For backup functionality (creates clone PVCs from snapshots)
  - Clone PVCs must be creatable via `dataSource: VolumeSnapshot`
  - Recommended: Use storage class with "Delete" reclaim policy for automatic clone cleanup
- **VolumeSnapshotClass configured** in cluster
- **BorgBackup repository** (e.g., BorgBase, self-hosted)

### Verification

```bash
# Check CSI driver supports snapshots
kubectl get volumesnapshotclass

# Check if your storage class can create snapshots
kubectl get storageclass <your-class> -o yaml | grep -i snapshot

# Test snapshot creation (optional)
kubectl create -f - <<EOF
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: test-snapshot
spec:
  source:
    persistentVolumeClaimName: <your-pvc>
  volumeSnapshotClassName: <your-snapshot-class>
EOF
```

## Features

- **CSI VolumeSnapshot Automation** - Create and prune snapshots with tiered retention (hourly/daily/weekly/monthly)
- **BorgBackup Integration** - Backup snapshot clones to offsite BorgBackup repository with deduplication and compression
- **Database Consistency Hooks** - Pod-exec hooks for PostgreSQL, MySQL, etc. (pg_backup_start/stop)
- **SIGTERM Safety** - Guaranteed cleanup and post-hook execution even on pod termination
- **Parallel Snapshots** - Create multiple snapshots simultaneously for fast backup
- **Privileged Container Support** - Backup any PVC regardless of ownership (PostgreSQL 70:70, MySQL 999:999, etc.)
- **Ephemeral Clone PVCs** - Temporary PVCs from snapshots for backup, auto-cleaned after
- **Configurable Timeouts** - Per-backup timeouts for clone provisioning and backup execution
- **Helm Deployment** - Single chart with separate CronJobs for snapshot and backup operations
- **Multi-Architecture** - ARM64 and AMD64 support

**Restore functionality** is currently in development.

## Quick Start

### Add Helm Repository

```bash
helm repo add kube-borg-backup https://frederikb96.github.io/kube-borg-backup
helm repo update
```

### View Configuration

```bash
# View all available options with inline documentation
helm show values kube-borg-backup/kube-borg-backup

# View complete Immich example with PostgreSQL hooks
curl -O https://raw.githubusercontent.com/frederikb96/kube-borg-backup/main/example/values.yaml
```

### Install

```bash
# Create your values.yaml based on example
# Then install:
helm install my-backup kube-borg-backup/kube-borg-backup \
  --namespace my-app \
  --values my-values.yaml

# Verify deployment
kubectl get cronjobs -n my-app
kubectl get serviceaccount,role,rolebinding -n my-app | grep borg
```

### Manual Test

```bash
# Manually trigger snapshot job
kubectl create job --from=cronjob/my-backup-kube-borg-backup-snapshot \
  snapshot-manual-test -n my-app

# Watch logs
kubectl logs -n my-app -l job-name=snapshot-manual-test -f

# Check created snapshots
kubectl get volumesnapshots -n my-app
```

## Configuration

All configuration is done via Helm values. See the following files for detailed documentation:

- **[charts/kube-borg-backup/values.yaml](charts/kube-borg-backup/values.yaml)** - All available configuration options with comprehensive inline comments
- **[example/values.yaml](example/values.yaml)** - Complete working example for Immich backup with PostgreSQL consistency hooks

## How It Works

1. **Snapshot Controller** runs as CronJob and creates `VolumeSnapshot` resources for configured PVCs
2. **Pre-hooks** execute sequentially before snapshots (e.g., `pg_backup_start()` to pause PostgreSQL writes)
3. **Snapshots** are created in parallel via CSI driver for instant point-in-time capture
4. **Post-hooks** execute sequentially after snapshots (e.g., `pg_backup_stop()` to resume writes)
5. **Tiered retention** prunes old snapshots keeping 1 per time bucket (hourly/daily/weekly/monthly)
6. **Backup Controller** runs as separate CronJob and finds latest snapshots
7. **Clone PVCs** are created from snapshots with temporary storage class
8. **Borg pods** are spawned dynamically (one per PVC) and run privileged to access any filesystem
9. **Backups** execute sequentially (Borg limitation) with configurable timeouts and lock handling
10. **Cleanup** happens automatically: borg pods deleted, clone PVCs removed, secrets cleaned up

**SIGTERM safety:** Both controllers have signal handlers ensuring post-hooks always run and resources are cleaned up even on pod eviction.

## Architecture

The tool consists of three components:

1. **Snapshot CronJob** - Python controller that creates/prunes VolumeSnapshots with pod-exec hooks
2. **Backup CronJob** - Python controller that creates clone PVCs and orchestrates Borg pods
3. **Borg Pods** - Ephemeral privileged pods spawned per backup to run actual `borg create` and `borg prune`

Both CronJobs use the unified `kube-borg-backup-controller` image (Python 3.13) with different entrypoints.
Borg pods use the `kube-borg-backup-essentials` image (Alpine + borgbackup + Python 3.13).

**Technologies:**
- Python 3.13 with kubernetes client library
- BorgBackup for deduplication and compression
- Kubernetes CSI VolumeSnapshot API
- Helm 3 for packaging

## Development

**Linting and Type Checking:**

```bash
# Set up testing venv
cd apps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt -r controller/requirements.txt

# Run linting
ruff check apps/

# Run type checking
mypy --config-file mypy.ini
```

## Contributing

Contributions welcome! Please:

1. Create issues for bugs or feature requests
2. Fork and submit pull requests
3. Follow existing code style (Ruff + Mypy enforced via CI)
4. Update CHANGELOG.md with your changes
5. Test in dedicated namespace in kubernetes before submitting

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Links

- **GitHub:** https://github.com/frederikb96/kube-borg-backup
- **Helm Charts:** https://frederikb96.github.io/kube-borg-backup
- **Issues:** https://github.com/frederikb96/kube-borg-backup/issues
- **Docker Images:** https://github.com/frederikb96?tab=packages
