# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] - 2025-10-22

### Added
- Initial release of kube-borg-backup
- Automated PVC snapshot creation and pruning via CSI VolumeSnapshots
- BorgBackup integration for offsite backup of snapshots
- Helm chart for easy deployment
- Support for customizable pre/post snapshot hooks (e.g., database pause/resume)
- Configurable retention policies for snapshots
- RBAC setup for snapshot and backup operations
- Multi-architecture container images (amd64, arm64)

### Changed
- Unified Python controller image combining snapshot and backup operations
- Renamed from `pvc-csi-snapshot-borgbackup-helm` to `kube-borg-backup`
- Restructured repository for cleaner organization

[Unreleased]: https://github.com/frederikb96/kube-borg-backup/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/frederikb96/kube-borg-backup/releases/tag/v1.0.0
