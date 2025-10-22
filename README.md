# kube-borg-backup

Kubernetes PVC snapshot and BorgBackup automation tool using CSI VolumeSnapshots.

## Features

- **Automated PVC Snapshots** - Create and prune CSI VolumeSnapshots on a schedule
- **BorgBackup Integration** - Automatically backup snapshots to BorgBackup repositories
- **Flexible Hooks** - Run custom scripts before/after snapshots (e.g., database pause/resume)
- **Retention Policies** - Keep N most recent snapshots and/or snapshots within M hours
- **Helm Deployment** - Single chart with separate CronJobs for snapshot and backup operations
- **Multi-Architecture** - Container images for amd64 and arm64

## Quick Start

```bash
# Add Helm repository
helm repo add kube-borg-backup https://frederikb96.github.io/kube-borg-backup
helm repo update

# View default values
helm show values kube-borg-backup/kube-borg-backup

# Install with your configuration
helm install my-backup kube-borg-backup/kube-borg-backup \
  --namespace my-app \
  --values my-values.yaml
```

## Configuration

See `example/values.yaml` for a complete Immich backup configuration including:
- PostgreSQL pause/resume hooks
- Multiple PVC backups (database, cache, data)
- Borg repository credentials
- Retention policies

Key configuration sections:

### Snapshot CronJob

```yaml
snapshot:
  name: my-snapshot
  image:
    repository: ghcr.io/frederikb96/kube-borg-backup-controller
    tag: latest
  cron:
    schedule: "0 * * * *"  # Hourly
  hooks:
    pre: |
      # Commands to run before snapshot (e.g., pause database)
    post: |
      # Commands to run after snapshot (e.g., resume database)
  snapshots:
    - pvc: my-data-pvc
      class: longhorn
      keep:
        n: 12          # Keep 12 most recent
        m_hours: 24    # Keep all from last 24 hours
```

### Backup CronJob

```yaml
borgbackup:
  name: my-backup
  cron:
    schedule: "30 */6 * * *"  # Every 6 hours
  pod:
    image: ghcr.io/frederikb96/kube-borg-backup-essentials:latest
  borgFlags: "--keep-hourly=24 --keep-daily=7 --keep-weekly=4"
  cache:
    create: true
    pvcName: borg-cache
    storageClassName: local-path
    size: 5Gi
  sshSecret:
    name: borg-ssh
    privateKey: |
      -----BEGIN OPENSSH PRIVATE KEY-----
      ...
      -----END OPENSSH PRIVATE KEY-----
  repoSecret:
    name: borg-secrets
    BORG_REPO: user@host:repo
    BORG_PASSPHRASE: your-passphrase
  backups:
    - name: database
      pvc: my-db-pvc
      class: longhorn
```

## Architecture

The tool consists of three components:

1. **Snapshot CronJob** - Python controller that creates/prunes VolumeSnapshots
2. **Backup CronJob** - Python controller that orchestrates Borg backups
3. **Borg Pod** - Ephemeral pod spawned by backup controller to run actual backup

Both CronJobs use the unified `kube-borg-backup-controller` image with different entrypoints.

## Requirements

- Kubernetes 1.25+
- CSI driver with VolumeSnapshot support
- VolumeSnapshotClass configured
- BorgBackup repository (e.g., BorgBase)

## Development

### Local Testing

Test chart changes without going through the release cycle:

```bash
# Lint the chart
helm lint charts/kube-borg-backup

# Test template rendering
helm template test charts/kube-borg-backup --values example/values.yaml

# Install directly from local directory to test namespace
helm upgrade --install kube-borg-backup-dev charts/kube-borg-backup \
  --namespace kube-borg-backup-dev \
  --create-namespace \
  --values example/values.yaml

# Watch deployment
kubectl get pods -n kube-borg-backup-dev -w

# Cleanup test
helm uninstall kube-borg-backup-dev -n kube-borg-backup-dev
kubectl delete namespace kube-borg-backup-dev
```

### Test Namespace Setup

**IMPORTANT:** Always use a dedicated test namespace to keep clean state and avoid impacting production:

```bash
# Create test namespace
kubectl create namespace kube-borg-backup-dev

# Create test PVC (example with simple data)
kubectl apply -f - <<EOF
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: test-data-pvc
  namespace: kube-borg-backup-dev
spec:
  accessModes: [ReadWriteOnce]
  storageClassName: your-storage-class
  resources:
    requests:
      storage: 1Gi
EOF

# Create test pod to write data
kubectl run test-writer --image=busybox --namespace=kube-borg-backup-dev \
  --restart=Never --rm -it -- sh -c "echo 'test data' > /data/test.txt"
```

**TODO:** Add detailed guide for setting up test database + app deployment in test namespace

### Building Images

```bash
# Build controller image
cd apps/controller
docker build -t ghcr.io/frederikb96/kube-borg-backup-controller:dev .

# Build essentials image
cd apps/essentials
docker build -t ghcr.io/frederikb96/kube-borg-backup-essentials:dev .
```

### Python Development

```bash
# Install dependencies
cd apps/controller
pip install -r requirements.txt

# Run snapshot tool directly
python -m kube_pvc_snapshot.main --config test-config.yaml

# Run backup tool directly
python -m kube_snapshot_borgbackup.main --config test-config.yaml
```

## Contributing

Contributions welcome! Please:

1. Create issues for bugs or feature requests
2. Fork and submit pull requests
3. Follow existing code style (PEP 8 for Python)
4. Update CHANGELOG.md with your changes
5. Test in dedicated namespace before submitting

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Links

- **GitHub:** https://github.com/frederikb96/kube-borg-backup
- **Helm Charts:** https://frederikb96.github.io/kube-borg-backup
- **Issues:** https://github.com/frederikb96/kube-borg-backup/issues
