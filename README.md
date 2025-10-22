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

All configuration is done via Helm values. See the following files for detailed documentation:

- **`charts/kube-borg-backup/values.yaml`** - All available configuration options with inline comments
- **`example/values.yaml`** - Complete working example for Immich backup with PostgreSQL hooks

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
