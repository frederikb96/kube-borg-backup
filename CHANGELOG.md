# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2025-10-24

**MAJOR RELEASE** - Complete rewrite with breaking changes. Not backwards compatible with 1.x.

### Breaking Changes

- **Values.yaml Structure Overhaul**
  - Removed `snapshot.name` and `snapshot.containerName` fields (now auto-generated from release name)
  - Removed `borgbackup.name`, `borgbackup.containerName`, `borgbackup.pod.name` fields
  - Removed `borgbackup.borgFlags` (replaced with structured `retention` object)
  - Removed separate `sshSecret` and `repoSecret` (consolidated into `borgbackup` config)
  - Added `snapshot.timeout` (CronJob activeDeadlineSeconds)
  - Added `borgbackup.timeout` (CronJob activeDeadlineSeconds)
  - Added per-backup `timeout` and `cloneBindTimeout` fields (both REQUIRED)
  - Changed retention from `keep.n` and `keep.m_hours` to tiered `retention.hourly/daily/weekly/monthly`
- **Config-Based Essentials Container**
  - Essentials now reads configuration from mounted YAML file instead of environment variables
  - Controller creates ephemeral config secrets dynamically per backup (no more pre-existing secrets)
- **Privileged Mode Default**
  - Borg pods now run privileged by default for universal PVC compatibility
  - Bypasses filesystem permission checks, works with any PVC ownership
  - Can be disabled via `borgbackup.pod.privileged: false`
- **Namespace Handling**
  - Controllers now require explicit `namespace` field in config (no more auto-detection)
  - Passed from Helm values to config secrets
- **Helm Naming Conventions**
  - Resource names now follow Helm standard: `{{ .Release.Name }}-kube-borg-backup`
  - Borg pod names include release name for better identification

### Added

- **Python 3.13 Support**
  - Upgraded both controller and essentials images to Python 3.13
  - Better performance and newer language features
- **Linting and Type Checking**
  - Added Ruff linter configuration (`.ruff.toml`)
  - Added mypy type checking configuration (`mypy.ini`)
  - GitHub workflows for automated linting (`ruff.yaml`, `mypy.yaml`)
  - Development requirements file (`apps/requirements-dev.txt`)
  - Testing venv at `apps/.venv` for both modules
- **Pod-Exec Hooks via Kubernetes API**
  - Pre/post hooks now use Kubernetes `stream.stream()` API for pod command execution
  - No kubectl binary needed in controller image (cleaner, smaller)
  - Per-PVC hook configuration with sequential execution
  - Structured hook format: `{pod: "name", container: "optional", command: [...]}`
- **SIGTERM Signal Handlers**
  - Both snapshot and borgbackup controllers handle SIGTERM gracefully
  - Guaranteed post-hook execution even on pod eviction
  - Guaranteed resource cleanup (clone PVCs, borg pods, secrets)
  - Essentials container handles SIGTERM: sends SIGINT to borg, waits 20s for checkpoint, force cleanup if needed
- **Tiered Retention Policy**
  - Snapshot retention now uses time-based bucketing (hourly/daily/weekly/monthly)
  - Keeps 1 snapshot per time bucket within retention window
  - Industry-standard approach matching Velero, Stash, AWS Backup
- **WaitForFirstConsumer Handling**
  - Clone PVC logic detects WaitForFirstConsumer storage classes
  - Waits for "WaitForFirstConsumer" event before spawning borg pod
  - Configurable `cloneBindTimeout` per backup
- **Ephemeral Secrets**
  - Controller creates ephemeral config secrets per backup run
  - SSH keys mounted from ephemeral secrets (automatically cleaned up)
  - Secrets only exist during backup execution (better security)
- **Python Essentials Container**
  - Essentials rewritten from shell script to Python (`run.py`)
  - Config file reading from `/config/config.yaml`
  - Proper signal handling as PID 1
  - Structured logging with Python logging module
  - Type hints and docstrings (PEP 257)
- **Configurable Timeouts**
  - Per-backup `timeout` controls both pod deadline and borg lock-wait
  - Per-backup `cloneBindTimeout` for clone PVC provisioning
  - Global CronJob timeouts for both snapshot and borgbackup controllers
- **RBAC Improvements**
  - Added `secrets` permission (create, get, list, delete) for ephemeral secret management

### Changed

- **BorgBackup Controller Complete Rewrite**
  - Pure Python pod manifest construction (removed Jinja2 templates)
  - Sequential backup execution (borg repository only supports one writer)
  - Continue-on-failure error handling (all backups attempted, failures reported at end)
  - Timezone-aware timestamps (`datetime.now(timezone.utc)`)
  - Structured logging with consistent format
  - Test mode (`--test` flag) for development
  - Global `_tracked_resources` dict for SIGTERM cleanup
- **Snapshot Controller Improvements**
  - Parallel snapshot creation using ThreadPoolExecutor
  - Proper exit codes (1 on failure, 0 on success)
  - Test mode with 5-second delay for SIGTERM testing
  - Timezone-aware datetime handling
  - Improved error messages and logging
- **Python Code Quality**
  - Modern type hints (`dict[str, Any]` instead of `Dict[str, Any]`)
  - Union types using `|` syntax (`str | None` instead of `Optional[str]`)
  - Type assertions for proper type narrowing
  - Explicit type annotations for collections
  - Fixed unreachable code
  - Removed unused variables
  - 120 character line length
- **Documentation**
  - Complete README.md rewrite with badges, prerequisites, quick start, concepts
  - New example/values.yaml with current structure (Immich use case preserved)
  - Updated CLAUDE.md with comprehensive testing workflows
  - Inline values.yaml documentation improvements

### Fixed

- **RBAC Permissions**
  - Added missing `secrets` permission to Role (controller couldn't create ephemeral secrets)
- **Storage Class Handling**
  - Fixed issues with `longhorn` vs `longhorn-normal` storage classes
  - Documentation now recommends WaitForFirstConsumer classes
- **Type Errors**
  - Fixed 13 mypy type checking errors
  - Added proper type narrowing with assertions
  - Added explicit type annotations where needed
- **Linting Issues**
  - Fixed 60+ ruff linting issues
  - Removed f-string without placeholders
  - Fixed deprecated typing imports
  - Fixed line length issues
- **Permission Issues**
  - Privileged mode fixes permission denied errors for PVCs with restrictive ownership
  - Preserves exact UID/GID for perfect restores
- **Python Output Buffering**
  - Added `PYTHONUNBUFFERED=1` to controller Dockerfile for real-time log streaming

### Removed

- **Jinja2 Templates**
  - Removed `pod-template.yaml.j2` (now pure Python dict construction)
  - Removed Jinja2 dependency from requirements.txt
- **Pre-Existing Secrets**
  - Removed `borg-ssh-secret.yaml` template
  - Removed `borg-repo-secret.yaml` template
  - Secrets now created ephemerally by controller
- **Deprecated Fields**
  - Removed `borgFlags` (use structured `retention` instead)
  - Removed custom name/containerName overrides (follow Helm conventions)
  - Removed global `pvcTimeout` (use per-backup `cloneBindTimeout`)
- **Shell-Based Essentials**
  - Old `run.sh` marked as DEPRECATED (replaced by `run.py`)

### Technical Details

- **Controller Image**: `ghcr.io/frederikb96/kube-borg-backup-controller:2.0.0` (Python 3.13-slim, 2 entrypoints)
- **Essentials Image**: `ghcr.io/frederikb96/kube-borg-backup-essentials:2.0.0` (Python 3.13-alpine + borgbackup)
- **Helm Chart**: `kube-borg-backup` v2.0.0
- **Kubernetes**: Requires 1.25+ with CSI VolumeSnapshot support
- **Testing**: Comprehensive test suite in `.claude/tests/` (Python direct, Docker, Helm)

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

[Unreleased]: https://github.com/frederikb96/kube-borg-backup/compare/v2.0.0...HEAD
[2.0.0]: https://github.com/frederikb96/kube-borg-backup/compare/v1.0.0...v2.0.0
[1.0.0]: https://github.com/frederikb96/kube-borg-backup/releases/tag/v1.0.0
