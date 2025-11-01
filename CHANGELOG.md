# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [6.0.9] - 2025-11-01

### Added

- **Configurable borg Flags per PVC:** New optional `borgFlags` field in `borgbackup.pvcs[]`
  - Default: `["--stats"]` (removes problematic `--list --filter=AME`)
  - Massive performance improvement: 30 min → 2-3 min backups for unchanged data
  - Fixes per-file processing overhead (1,669 files × ~1s = 28 min eliminated)
  - Configurable per PVC: `borgFlags: []` for max speed, `borgFlags: ["--stats", "--progress"]` for visibility
  - Files: values.yaml, example/values.yaml, borgbackup-config-secret.yaml, controller/main.py, backup-runner/backup.py

- **Network Stats in Heartbeat:** Heartbeat now shows network I/O delta
  - Displays `Net: +X.XMB` every 60 seconds
  - Helps identify network-bound operations
  - Gracefully handles systems without network stats support
  - File: backup-runner/backup.py

- **Borg Lock Check:** Pre-flight repository lock status check before backup
  - Runs `borg with-lock --lock-wait 0` to check lock status
  - Logs: unlocked/locked/timeout/error
  - Non-blocking diagnostic (always continues)
  - 10s timeout prevents hanging
  - File: backup-runner/backup.py

### Changed

- **Default borg Flags Performance Fix:** Removed `--list --filter=AME` from defaults
  - Previous: caused ~1s overhead per file (network metadata checks)
  - New default: `["--stats"]` only
  - Performance: 30 min → 2-3 min for incremental backups

## [6.0.8] - 2025-11-01

### Added

- **Cache-The-Cache Feature:** Performance optimization for borg backup operations
  - New Helm values: `cache.cacheTheCache` (default false) enables ephemeral local cache
  - Startup: rsync cache from PVC to `/tmp/borg-cache-local/` for faster access
  - Operations: Borg uses local ephemeral storage instead of PVC-mounted cache (reduced I/O latency)
  - Normal shutdown: rsync back to PVC with summary stats (quiet mode)
  - SIGTERM shutdown: rsync back with verbose file-by-file progress for debugging
  - Configurable `cache.accessModes` (default ReadWriteOncePod) to prevent concurrent pod access
  - Aborts backup if startup rsync fails, exits with error if shutdown rsync fails
  - No timeouts on rsync operations (controller handles pod termination)
  - Documentation in values.yaml explains inconsistency risk and single-pod requirement
  - Files: `backup.py` (3 new functions), `common.py` (cache_dir parameter), controller `main.py` (6 locations), Helm templates
  - Commit: [commit hash]

### Changed

- **Signal Handling Grace Period:** Reduced from 20s to 10s for borg checkpoint wait
  - Faster termination on SIGTERM/SIGINT while still allowing graceful checkpoint
  - File: `apps/backup-runner/backup.py` (line 259)

### Fixed

- **Controller Secret Creation Bug:** Fixed cache-the-cache flag not passed to backup-runner pods
  - Root cause: `create_borg_secret()` wasn't including `cacheTheCache` in dynamic Secret
  - Added `cache_the_cache` parameter through entire call chain (6 locations)
  - Bug prevented feature from activating even when enabled in values.yaml
  - File: `apps/controller/kube_snapshot_borgbackup/main.py` (lines 355, 383, 923, 999, 1080, 1117)

## [6.0.7] - 2025-11-01

### Fixed

- **Pod Monitoring Event Deduplication:** Eliminated repeating event output
  - **Bug:** Watch timeout reconnects (every 60s) replayed ALL historical pod events
  - **Symptom:** Same events (Scheduled, FailedAttachVolume, Pulling, etc.) repeated 4+ times throughout pod lifecycle
  - **Root cause:** No deduplication mechanism in `apps/common/pod_monitor.py` `_stream_events()` method
  - **Fix:** UID-based deduplication using event UIDs tracked in memory
  - **Why UID approach:** Simpler and more robust than resourceVersion tracking, handles duplicates from ALL sources (timeout, 410 errors, network drops), no race conditions
  - **Benefits:** Clean event output, enhanced error handling (network interruptions), quieter logging (410 errors are normal)
  - Memory efficient: ~50-100 UIDs per pod (~4KB)
  - File: `apps/common/pod_monitor.py` (lines 157-222)

- **Container Naming Consistency:** Renamed backup job container from "borg" to "backup-runner"
  - Pod name: `{release}-borg-{name}-{timestamp}` → `{release}-backup-runner-{name}-{timestamp}`
  - Container name: `"borg"` → `"backup-runner"`
  - Better reflects the actual image name and purpose
  - File: `apps/controller/kube_snapshot_borgbackup/main.py` (lines 597, 989)

## [6.0.6] - 2025-11-01

### Fixed

- **CLI Backup Restore PVC Matching Logic:** Critical fix for archive prefix matching
  - **Bug:** `rsplit('-', 3)[0]` only removed last 3 parts, but timestamp has 6 parts when split by `-`
  - Example: `kimai-data-2025-10-31-18-33-28` → `kimai-data-2025-10-31` (wrong!) instead of `kimai-data`
  - **Fix:** Use `startswith()` check instead of extracting prefix
  - Algorithm: `archive.startswith(backup_name + '-')` for each configured backup
  - Much simpler, more robust, works regardless of timestamp format
  - File: `apps/cli/kbb/commands/backup.py` (lines 387-410)
  - Commit: f7e8a06

- **CLI Linting:** Removed unused signal handler variables
  - Fixed ruff F841 errors: `old_sigterm`, `old_sigint`, `old_sighup` assigned but never used
  - Not needed since CLI process exits after restore completes
  - File: `apps/cli/kbb/restore_helpers.py` (lines 206-208)
  - Commit: 783a78c

### Changed

- **CI/CD: Release Pipeline Quality Gates:** Enforced linting and type checking before releases
  - Consolidated 3 separate workflows into single `release-pipeline.yaml`
  - Added job dependencies using GitHub Actions `needs` keyword
  - Step 1 (parallel): `lint` (ruff), `typecheck` (mypy), `helm-lint` (helm lint)
  - Step 2 (only if ALL pass): `release-helm`, `build-controller`, `build-backup-runner`
  - **Zero chance** of releasing with linting, type, or Helm chart errors
  - Atomic release process (all or nothing)
  - Removed: `release.yaml`, `controller-image.yaml`, `backup-runner-image.yaml`
  - Created: `.github/workflows/release-pipeline.yaml`
  - Commits: abf2756, 4aafb95

## [6.0.5] - 2025-10-31

### Fixed

- **CLI Backup Restore PVC Selection:** Critical bug fix for wrong PVC selection
  - **Bug:** `kbb backup restore kimai-data-2025-10-31-18-33-28` restored to wrong PVC (`kimai-mysql-pvc` instead of `kimai-data`)
  - **Root cause:** Always used first backup's PVC (`backups[0]['pvc']`), ignored archive name
  - **Fix:** Parse archive prefix from name, find matching backup config entry, use its `pvc` field
  - Archive format: `{prefix}-YYYY-MM-DD-HH-MM-SS` where prefix = `backups[].name` from config
  - Auto-detection message shows which prefix matched: `Auto-detected target PVC from archive prefix 'kimai-data': kimai-data`
  - Clear error messages when archive doesn't match any configured backup (lists available backups)
  - Supports custom `archivePrefix` (fails gracefully with helpful message)
  - User can always override with `--pvc` flag
  - File: `apps/cli/kbb/commands/backup.py` (lines 388-427)

- **CLI PVC Validation:** Added early validation for both restore operations
  - Verify target PVC exists before spawning pods or creating clone PVCs
  - Prevents wasted time on non-existent targets
  - Clear error messages: `Error: Target PVC 'name' not found in namespace 'ns'`
  - Files: `apps/cli/kbb/commands/backup.py` (lines 417-425), `apps/cli/kbb/commands/snapshot.py` (lines 197-209)

## [6.0.4] - 2025-10-31

### Added

- **CLI Signal Handling:** Complete SIGTERM/SIGINT/SIGHUP handling for CLI operations
  - Graceful cleanup of spawned pods on Ctrl+C or termination
  - 30-second grace period for pod termination, force delete fallback
  - Applies to all 3 operations: `kbb backup list`, `kbb backup restore`, `kbb snapshot restore`
  - User-friendly progress messages during cleanup
  - Two-layer cleanup synergy: CLI (30s) + backup-runner (20s) = robust termination
  - Exit code 143 for signal-induced termination
  - Files: `apps/cli/kbb/commands/backup.py`, `apps/cli/kbb/commands/snapshot.py`, `apps/cli/kbb/restore_helpers.py`

- **Backup-Runner Signal Handling:** SIGTERM/SIGINT handling for list and restore operations
  - `list.py`: Catches SIGTERM, sends SIGINT to borg, waits 20s, force kill + break-lock fallback
  - `restore.py`: Catches SIGTERM, terminates rsync + borg mount, unmounts FUSE, breaks locks if needed
  - Prevents stale borg locks on pod termination
  - Matches backup.py signal handling pattern (already present)
  - Files: `apps/backup-runner/list.py`, `apps/backup-runner/restore.py`

### Improved

- **Pod Termination Detection:** Simplified to Kubernetes best practices (KISS principle)
  - Changed from checking `deletionTimestamp` (unreliable) to polling for 404 from API
  - 404 is the only definitive signal that Kubernetes fully removed pod
  - Removed ~30 lines of unnecessary complexity
  - Better error logging for non-404 API errors
  - Matches official Kubernetes Python client documentation
  - Applies to all 3 CLI cleanup functions

### Changed

- **README:** CLI installation instructions updated
  - Removed hardcoded version `v6.0.0`, now uses pattern `@vX.Y.Z`
  - Added alternative: install from `main` branch (development version)
  - Fixed upgrade command: use `pipx install --force` (git installs don't support `pipx upgrade`)
  - Added link to releases page
  - README now version-agnostic

## [6.0.3] - 2025-10-31

### Fixed

- **Mypy CI Pipeline:** Fixed mypy type checking failure in GitHub Actions
  - Deleted unused `apps/backup-runner/__init__.py` (hyphen in directory name invalid for Python package)
  - Updated `mypy.ini` to check individual .py files instead of package directory
  - No functional changes - backup-runner scripts run standalone in Docker, never imported as module
  - CI now passes: 8 source files checked successfully
  - File changes: `apps/backup-runner/__init__.py` (deleted), `mypy.ini` (updated files directive)

## [6.0.2] - 2025-10-30

### Fixed

- **Borg Restore Hooks:** Fixed `borgbackup-config-secret.yaml` template missing restore section
  - Template was not rendering restore hooks to borg-config Secret
  - Caused borg restore to skip pre/post hook execution (leaving apps scaled down)
  - Added restore section rendering (matching snapshot-config-secret.yaml pattern)
  - Hooks now properly included: pod image config + preHooks + postHooks
  - File: `charts/kube-borg-backup/templates/borgbackup-config-secret.yaml`

## [6.0.1] - 2025-10-30

### Fixed

- **Branch Strategy:** Re-released v6.0.0 from `main` branch
  - Original v6.0.0 was released from `dev` branch
  - Used two-step merge strategy to bring v6.0.0 to `main`
  - No code changes from v6.0.0 - purely organizational

## [6.0.0] - 2025-10-30

### Added

- **Restore Functionality (Production-Ready)** (CLI v1.0.0)
  - CLI tool installable via pipx for restore operations
  - Snapshot restore: Clone VolumeSnapshot to new PVC or rsync to existing PVC
  - Borg archive restore: FUSE mount support for flexible restore workflows
  - Pre/post hooks for restore: Scale deployments, execute commands (exec hooks)
  - Parallel and sequential hook execution modes
  - Data integrity validation in restore tests (SHA256 checksums)
  - Configurable image tags read from config Secret (production-ready)

- **Restore Helper Pods** (Helm Chart v6.0.0)
  - Rsync helper pods for snapshot restore data copy (uses backup-runner image)
  - FUSE mount pods for borg archive restore
  - `snapshot.pod.image` configuration for rsync helper pods
  - Self-contained architecture (no external Alpine image dependency)

- **Test Data Validation** (Testing v6.0.0)
  - SHA256 checksum validation in all restore tests
  - Ensures data integrity across restore workflows
  - See DATA_VALIDATION.md for methodology

- **Heartbeat Monitoring for Long-Running Backups** (Backup-Runner v6.0.0)
  - Progress monitoring during silent deduplication phases
  - Prints heartbeat every 60 seconds with CPU, I/O, and memory metrics
  - Tracks delta changes between heartbeats for activity visibility
  - Essential for large backups (multi-TB) where borg is silent for hours
  - Uses psutil for lightweight process monitoring

### Changed

- **CRITICAL: Removed Timeout Limits from Restore Operations** (CLI v1.0.0)
  - Restore operations now run indefinitely until completion or manual cancellation
  - Previous timeouts (120-300s) were blocking large volume restores
  - Essential for production use with multi-TB volumes
  - Applies to: snapshot restore, borg restore, all restore hooks

- **Storage Class Alignment** (Helm Chart v6.0.0, Testing v6.0.0)
  - Aligned clone PVC storage class to `longhorn-normal` (WaitForFirstConsumer)
  - Updated all test configs and documentation
  - Follows CLAUDE.md best practices

- **Image Configuration** (Helm Chart v6.0.0, CLI v1.0.0)
  - CLI reads image tags from config Secret (no hardcoded `:dev` tags)
  - Added `snapshot.pod.image` to values.yaml defaults
  - Rsync operations use backup-runner image instead of external Alpine
  - Configurable via Helm values for production deployments

### Fixed

- **CRITICAL: Event Streaming Infinite Loop in Long-Running Backups** (Controller v6.0.0)
  - Fixed bug in `apps/common/pod_monitor.py` causing infinite event replay
  - Was using individual Event object's resource_version instead of watch list's resource_version
  - Triggered on 60-second watch timeout, causing massive log spam in long-running backups (Immich)
  - Events would replay infinitely: Scheduled → FailedAttachVolume → SuccessfulAttachVolume (30+ times)
  - Fix: Use watch response metadata's resource_version for proper resumption
  - Affects: v5.0.7, v5.0.8 (only visible in backups >60s runtime)

- **Production-Ready CLI** (CLI v1.0.0)
  - Removed hardcoded `:dev` image tags (replaced with config-driven tags)
  - CLI now suitable for production use without code changes

- **Test Reliability** (Testing v6.0.0)
  - Test scripts properly wait for dummy deployment before restore
  - Fixed timing issues in hook tests

### Documentation

- **Longhorn Namespace Hardcoding** (Documentation v6.0.0)
  - Added detailed explanation in CLAUDE.md (Phase A)
  - Explains why `longhorn-system` namespace is hardcoded
  - Documents reliability vs flexibility tradeoff

- **Data Validation** (Documentation v6.0.0)
  - Added DATA_VALIDATION.md explaining test integrity validation
  - Documents SHA256 checksum methodology

- **README Update** (Documentation v6.0.0)
  - Updated README to reflect restore functionality completion
  - Added CLI installation instructions
  - Removed "in development" status

### Breaking Changes

None - all changes are backward compatible or fixes

---

## [5.0.8] - 2025-10-28

### Fixed

- **CRITICAL: Clone PVC Creation Failure for Long PVC Names**
  - Removed unused `pvc` label from clone PVCs that was causing 422 errors
  - Kubernetes label values limited to 63 characters, clone names could exceed this (e.g., HedgeDoc: 65 chars)
  - Label was unused in codebase (cleanup uses `_tracked_resources` list, not labels)
  - Affects apps with long PVC base names (hedgedoc-uploads-pvc-enc, hedgedoc-postgres-pvc-enc)
  - Fix: apps/controller/kube_snapshot_borgbackup/main.py:324-328

## [5.0.7] - 2025-10-26

### Added

- **Longhorn Volume Readiness Detection** (controller v5.0.7)
  - Automatically detects Longhorn CSI volumes via PV driver inspection
  - Waits for Longhorn volume to reach `attached+healthy` state before spawning borg pods
  - Additional 15-second grace period for CSI workload readiness
  - Prevents "volume not ready for workloads" pod mount failures
  - RBAC: Added `longhorn.io/volumes` get/list permissions to ServiceAccount

### Improved

- **Clean Longhorn Polling Output** (controller v5.0.7)
  - Removed verbose status messages during Longhorn volume polling
  - Silently polls every 2 seconds, only logs when volume is ready or times out
  - Cleaner, less spammy log output

## [5.0.6] - 2025-10-26

### Added

- **Dual-Thread Real-Time Streaming** (controller v5.0.6)
  - Implemented concurrent log and event streaming for borg backup pods
  - Logs stream with `[pod-name]` prefix, events with `[EVENT]` prefix
  - Real-time visibility into pod execution, image pulls, volume attachment, container lifecycle

### Fixed

- **Log Streaming "Bad Request" Errors** (controller v5.0.6)
  - Fixed premature log streaming that occurred before container logs were ready
  - Now waits for `container.state.running.started_at` before streaming
  - Single `follow=True` call captures complete log output from start to finish
  - Eliminated HTTP 400 errors and log loss on fast-completing pods

- **Event Streaming Duplicates** (controller v5.0.6)
  - Fixed duplicate events replaying after 60-second watch timeout
  - Implemented `resourceVersion` tracking across reconnections
  - Events now stream continuously without duplicates
  - Seamless reconnection handling with no visible interruption

### Improved

- **Clean Output Format** (controller v5.0.6)
  - Removed verbose "waiting for logs" and "reconnecting" messages
  - Professional, production-ready log format
  - Clean interleaved event and log streams

## [5.0.5] - 2025-10-26

### Note
- Version-only release (no code changes) - actual streaming implementation released in v5.0.6

## [5.0.4] - 2025-10-25

### Fixed

- **Borg Info Timeout on Large Repositories** (backup-runner v5.0.4)
  - Skip initial `borg info` check at startup to avoid timeouts on large repos
  - Now attempts `borg create` directly for faster backups
  - Falls back to `borg info` + repo initialization only if create fails with exit code 2
  - Reduces backup time by 5-10 seconds in normal operations
  - Fixes timeout issues with repositories containing many archives

## [5.0.3] - 2025-10-25

### Fixed

- **Critical Multi-App Config Mutation Bug** (Helm Chart v5.0.3)
  - Fixed `mergeOverwrite` mutating shared defaults across app iterations
  - Bug caused ALL apps to use the LAST app's cache PVC name and other config values
  - Symptom: `kbb-immich-borg-cache` PVC created in wrong namespaces (kimai, immich)
  - Root cause: Helm's `mergeOverwrite` modifies first argument in-place
  - Solution: Use `deepCopy` before merging to prevent mutation
  - Affected helpers: `mergeBorgConfig` and `mergeSnapshotConfig`
  - Impact: Multi-app deployments now use correct per-app configuration

## [5.0.2] - 2025-10-25

### Fixed

- **Complete Multi-App Template Fix** (Helm Chart v5.0.2)
  - Fixed ALL remaining template files with missing newline before YAML separators
  - v5.0.1 only fixed 2 files; v5.0.2 fixes ALL 7 template files
  - Affected files:
    - `borg-cache-pvc.yaml` - Cache PVC creation
    - `borg-cronjob.yaml` - Borgbackup CronJob
    - `cronjob.yaml` - Snapshot CronJob
    - `namespace.yaml` - Namespace creation
    - `rbac.yaml` - RBAC resources
    - (plus previously fixed: `borgbackup-config-secret.yaml`, `snapshot-config-secret.yaml`)
  - All multi-app deployments now work correctly

## [5.0.1] - 2025-10-25

### Fixed

- **Critical Multi-App Template Bug** (Helm Chart v5.0.1)
  - Fixed missing newline before YAML document separator (`---`) in template files
  - Affected: `borgbackup-config-secret.yaml` and `snapshot-config-secret.yaml`
  - Bug caused YAML parsing errors when deploying multiple apps
  - Error: `yaml: unmarshal errors: line X: mapping key "apiVersion" already defined`
  - Solution: Added explicit newline (`{{ "" }}`) before closing `{{- end }}` tag
  - Impact: Multi-app deployments now work correctly without duplicate key errors

## [5.0.0] - 2025-10-25

### BREAKING CHANGES

**This release completely refactors resource naming to follow Helm best practices. All resources now include the release name as a prefix, enabling multiple releases (dev/staging/prod) in the same cluster.**

**Migration Required:**
- All resource names will change on upgrade
- Existing deployments will see resources recreated
- Plan for brief downtime during upgrade
- Review resource names before upgrading

**Old naming (v4.x):**
```
ServiceAccount: kbb
ClusterRole: kbb-clusterrole
CronJob: kbb-test-snapshot
```

**New naming (v5.0.0):**
```
ServiceAccount: {release-name}-sa
ClusterRole: {release-name}-clusterrole
CronJob: {release-name}-{app}-snapshot
```

**Example with release name `kbb-dev`:**
```
ServiceAccount: kbb-dev-sa
ClusterRole: kbb-dev-clusterrole
CronJob: kbb-dev-test-snapshot
Secret: kbb-dev-test-borg-config
```

### Added

- **Modern Helm Naming Pattern** (Helm Chart v5.0.0)
  - New `rbacName` helper: `{release-name}-{resource}` for RBAC resources
  - New `appResourceName` helper: `{release-name}-{app}-{resource}` for app resources
  - New `appBaseName` helper: `{release-name}-{app}` for Python controller config
  - All resources now include release name prefix (except user-specified cache PVC)
  - Enables multiple releases in same cluster (e.g., kbb-dev, kbb-prod, kbb-staging)

### Changed

- **All RBAC Resources** (Helm Chart v5.0.0)
  - ClusterRole: `kbb-clusterrole` → `{release-name}-clusterrole`
  - ClusterRoleBinding: `kbb-{namespace}-clusterrolebinding` → `{release-name}-clusterrolebinding-{namespace}`
  - ServiceAccount: `kbb` → `{release-name}-sa`
  - Role: `kbb-role` → `{release-name}-role`
  - RoleBinding: `kbb-rolebinding` → `{release-name}-rolebinding`

- **All App Resources** (Helm Chart v5.0.0)
  - CronJobs: `kbb-{app}-{type}` → `{release-name}-{app}-{type}`
  - Secrets: `kbb-{app}-{type}-config` → `{release-name}-{app}-{type}-config`
  - Example: `kbb-immich-snapshot` → `kbb-dev-immich-snapshot`

- **Python Controller Config** (Helm Chart v5.0.0)
  - `releaseName` field now uses `appBaseName` helper
  - Old: `releaseName: kbb-test-borg` (had resource type suffix)
  - New: `releaseName: kbb-dev-test` (no resource type suffix)
  - Python controller adds resource type when creating pods/secrets

### Fixed

- **Double-Borg Naming Issue** (Helm Chart v5.0.0)
  - Pod names had duplicate "borg": `kbb-test-borg-borg-postgres-data-{ts}`
  - Fixed by using `appBaseName` helper instead of `appResourceName`
  - New pod names: `kbb-dev-test-borg-postgres-data-{ts}` ✅

- **Multi-Release Support** (Helm Chart v5.0.0)
  - Fixed ClusterRole naming conflict preventing multiple releases
  - Can now deploy kbb-dev and kbb-prod in same cluster
  - Each release has its own isolated set of cluster-scoped resources

### Notes

- **Cache PVC:** Remains user-specified (NO release name prefix)
- **Python Controller:** No code changes - automatically follows new naming from config
- **Backward Compatibility:** NONE - this is a breaking change, plan upgrade carefully

## [4.2.0] - 2025-10-25

### Added

- **Optional `archivePrefix` per PVC** (Helm Chart v4.2.0)
  - New optional field `archivePrefix` in borgbackup.pvcs configuration
  - Allows custom borg archive prefix per backup instead of default `{app-name}-{backup-name}`
  - If omitted, uses default naming: `test-postgres-data-2025-10-25-14-30-00`
  - If set, uses custom prefix directly: `my-custom-backup-2025-10-25-14-30-00`
  - Useful for maintaining existing archive names during migration or matching legacy naming schemes
  - Example: `archivePrefix: legacy-db-backup`

- **Cache PVC Documentation** (Helm Chart v4.2.0)
  - Added inline documentation for cache PVC behavior
  - Clarifies: `create: true` → creates new PVC with name `pvcName`
  - Clarifies: `create: false` → uses existing PVC with name `pvcName` (must exist)
  - Documents which fields only apply when `create: true` (storageClassName, size)

### Changed

- **Hardcoded ServiceAccount Name** (Helm Chart v4.2.0)
  - Removed `serviceAccount.name` from values.yaml (was configurable, always set to `kbb`)
  - ServiceAccount name now hardcoded to `kbb` via helper function `kube-borg-backup.serviceAccountName`
  - Simplifies configuration (one less field to set)
  - **BREAKING:** If you customized `serviceAccount.name`, it will be ignored and reset to `kbb`

### Fixed

- **Cache PVC Naming** (Helm Chart v4.2.0)
  - Reverted accidental `kbb-` prefix on cache PVC names from v4.1.0
  - Cache PVC now uses `pvcName` directly without any prefix
  - Maintains backward compatibility with existing cache PVCs
  - Example: `kimai-borg-cache` not `kbb-kimai-borg-cache`

## [4.1.0] - 2025-10-25

### Added

- **Storage Class Validation** (Controller v3.1.0)
  - Added `validate_storage_class()` function to check storage class exists before creating clone PVC
  - Catches configuration errors early (e.g., typo in storage class name)
  - Clear error messages: "Storage class 'xyz' not found"
  - Prevents clone PVCs from staying Pending forever with cryptic errors

- **Enhanced PVC Error Logging** (Controller v3.1.0)
  - `wait_clone_pvc_ready()` now checks PVC events every 10 seconds during wait
  - New `_check_pvc_events_for_errors()` helper surfaces actual provisioning failures
  - Detects errors immediately instead of after full timeout (300s → ~10s)
  - Example: "❌ PVC provisioning failed: storageclass.storage.k8s.io 'longhorn-tmp' not found"
  - Searches for keywords: ProvisioningFailed, not found, failed, error, cannot, unable

- **Optimized Parallel Clone Creation** (Controller v3.1.0)
  - Phase 1: Submit ALL clone PVC creation requests in parallel (non-blocking)
  - Phase 2: Process backups sequentially, waiting for each clone individually before its backup
  - First backup starts as soon as first clone is ready (no wait for all clones)
  - While backup N runs, clones N+1, N+2, etc. continue provisioning in background
  - Performance improvement scales with number of PVCs
  - New `ClonePVC` dataclass to track clone state across phases
  - New `create_single_clone_pvc()` for parallel execution via ThreadPoolExecutor

- **ClusterRole for Storage Classes** (Helm Chart v4.1.0)
  - Added ClusterRole with `storage.k8s.io/storageclasses` read permissions
  - ClusterRoleBinding created per-namespace for each ServiceAccount
  - Required for new storage class validation feature
  - Minimal cluster-scoped permissions (get, list only)

### Changed

- **Backup Orchestration Flow** (Controller v3.1.0)
  - Refactored `main()` to use two-phase approach
  - Moved clone creation out of `process_backup()` into separate `create_all_clone_pvcs()`
  - Renamed `process_backup()` → `process_backup_with_clone()` to clarify it receives ready clone
  - Each backup now waits for its specific clone only, not all clones upfront
  - Better separation of concerns: provisioning vs backup execution

### Fixed

- **Ruff Linting** (Controller v3.1.0)
  - Removed unnecessary f-string prefix from log message without placeholders
  - All ruff checks now pass
  - Mypy type checking still passes (strict mode)

## [4.0.0] - 2025-10-25

**MAJOR RELEASE** - Multi-application architecture with breaking changes. Not backwards compatible with 3.x.

### Breaking Changes

- **Helm Values Structure Complete Overhaul**
  - Removed top-level `namespace` field (now per-app)
  - Added `borgRepos[]` list for reusable repository definitions
  - Added `apps[]` list for multi-application support
  - Moved all app-specific config under `apps[].snapshot` and `apps[].borgbackup`
  - Changed `borgbackup.borgRepo/borgPassphrase/sshPrivateKey` to reference `borgRepos[]` by name
  - Changed `serviceAccount.name` default from `kube-borg-backup` to `kbb` (shorter)
  - Borg archive names now auto-prefixed with app name (e.g., `immich-db-2025-10-25-14-30-00`)

- **Resource Naming Convention**
  - All resources now named `kbb-{app-name}-{resource-type}` instead of `{release-name}-{type}`
  - Examples: `kbb-immich-snapshot`, `kbb-immich-borgbackup`, `kbb-immich-borg-cache`
  - Enables multi-app deployments in single Helm release
  - DNS-safe app names enforced (lowercase alphanumeric + hyphens)

- **RBAC Resources**
  - ServiceAccount/Role/RoleBinding now created per-namespace (deduplicated if apps share namespace)
  - All use shortened `kbb` name by default instead of `kube-borg-backup`

### Added

- **Multi-Application Support**
  - Single Helm deployment can now backup multiple applications
  - Each app can have its own namespace, PVCs, schedules, and retention policies
  - Apps can share borg repositories or use separate ones
  - Per-app cache PVCs with unique names

- **Reusable Borg Repository Definitions**
  - Define borg repositories once in `borgRepos[]`
  - Reference by name in `apps[].borgbackup.borgRepo`
  - Share repositories across multiple apps
  - Credentials stored centrally, resolved per-app in templates

- **App Name Prefixing**
  - Borg archive names automatically prefixed with app name
  - Format: `{app-name}-{backup-name}-{timestamp}`
  - Example: `immich-db-2025-10-25-14-30-00` (app=immich, backup=db)
  - Prevents name collisions when multiple apps use same repository
  - Retention pruning automatically scoped per-app

- **Per-App Configuration Overrides**
  - Global defaults for `snapshot` and `borgbackup` in root values
  - Per-app overrides in `apps[].snapshot` and `apps[].borgbackup`
  - Uses `mergeOverwrite` for proper precedence (app config wins)
  - Supports overriding schedules, retention, timeouts, images, etc.

- **Namespace Management**
  - Optional `apps[].createNamespace` flag per app
  - Apps can target different namespaces
  - RBAC automatically created in all unique namespaces

- **Helper Functions**
  - `kube-borg-backup.validateAppName` - DNS-safe name validation
  - `kube-borg-backup.validateSnapshotConfig` - Validates required snapshot.pvcs field
  - `kube-borg-backup.validateBorgConfig` - Validates required borgbackup fields (cache, pvcs, borgRepo)
  - `kube-borg-backup.resourceName` - Consistent `kbb-{app}-{resource}` naming
  - `kube-borg-backup.mergeSnapshotConfig` - Default value merging for snapshots
  - `kube-borg-backup.mergeBorgConfig` - Default value merging for borgbackup
  - `kube-borg-backup.resolveBorgRepo` - Repository credential resolution
  - `kube-borg-backup.uniqueNamespaces` - Namespace deduplication for RBAC

- **Field Naming Improvements**
  - Renamed `borgbackup.backups` → `borgbackup.pvcs` for clarity (values.yaml user-facing)
  - Renamed `snapshot.pvcs` to match naming convention
  - Templates still map to controller's expected `backups` field internally
  - More intuitive: users configure PVCs, not abstract "backups"

- **Validation & Error Messages**
  - Added validation for required per-app fields
  - Clear error messages instead of cryptic template errors
  - Examples: "App 'myapp': borgbackup.cache is REQUIRED but not specified (must be unique per-app)"
  - Prevents confusing nil pointer errors

### Changed

- **Template Architecture**
  - All 7 templates rewritten to loop over `apps[]`
  - Each template generates resources per-app with proper naming
  - RBAC template deduplicates per-namespace instead of per-release
  - Config secrets now include app-prefixed backup names

- **Default Values**
  - ServiceAccount name shortened to `kbb`
  - Snapshot and borgbackup configs moved to root as defaults
  - **Removed defaults for per-app fields** (`cache`, `pvcs`, `borgRepo`) - must be specified per-app
  - Users must define `snapshot.pvcs` and `borgbackup.pvcs` for each app (prevents config errors)
  - Added example single-app structure in default values.yaml with PostgreSQL hooks

- **Example Configuration**
  - Updated `example/values.yaml` to new structure
  - Shows single Immich app with all features (PostgreSQL with hooks, multi-PVC backup)
  - Demonstrates borgRepo reference pattern
  - Includes app name prefixing comments
  - Verbose inline documentation explaining hooks, timeouts, and archive naming
  - Clear explanations of sequential vs parallel execution

- **Test Configuration**
  - Updated `.claude/tests/config/values.yaml` to new structure
  - Single test app targeting `kube-borg-backup-dev` namespace
  - Uses `borgbase-test` repository reference

### Migration Guide

**COMPLETE VALUES.YAML REWRITE REQUIRED**

This is not a simple field rename - the entire structure has changed. See migration steps:

#### Step 1: Define Borg Repositories

```yaml
# NEW (v4.0.0)
borgRepos:
  - name: borgbase-main
    repo: "ssh://user@borgbase.com/./repo"
    passphrase: "your-passphrase"
    privateKey: |
      -----BEGIN OPENSSH PRIVATE KEY-----
      ...
      -----END OPENSSH PRIVATE KEY-----
```

#### Step 2: Move Global Defaults

```yaml
# OLD (v3.0.0)
namespace:
  name: immich

snapshot:
  cron:
    schedule: "0 * * * *"
  # ... other snapshot config

borgbackup:
  cron:
    schedule: "30 */6 * * *"
  borgRepo: "ssh://..."
  borgPassphrase: "..."
  sshPrivateKey: |
    ...
  # ... other borgbackup config

# NEW (v4.0.0)
snapshot:
  # Same fields, but now global defaults
  cron:
    schedule: "0 */4 * * *"  # Default for all apps

borgbackup:
  # Same fields, but now global defaults
  cron:
    schedule: "30 */6 * * *"  # Default for all apps
  # Remove borgRepo/borgPassphrase/sshPrivateKey - now in borgRepos[]
```

#### Step 3: Create Apps List

```yaml
# NEW (v4.0.0)
apps:
  - name: immich  # CRITICAL: Used as archive prefix
    namespace: immich
    createNamespace: false

    snapshot:
      # Override schedule if needed
      cron:
        schedule: "0 * * * *"  # Hourly for this app
      pvcs:
        # Same structure as v3.0.0

    borgbackup:
      borgRepo: borgbase-main  # Reference borgRepos[].name
      cache:
        create: true
        pvcName: immich-borg-cache  # Must be unique per-app
        storageClassName: openebs-hostpath
        size: 5Gi
      backups:
        # IMPORTANT: Remove app prefix from names
        # OLD: name: immich-db
        # NEW: name: db (will become "immich-db" in archive)
        - name: db
          pvc: immich-db-pvc
          class: longhorn
          timeout: 7200
          cloneBindTimeout: 300
```

#### Step 4: Update ServiceAccount References

If you have RBAC references in other resources:

```yaml
# OLD (v3.0.0)
serviceAccountName: kube-borg-backup

# NEW (v4.0.0)
serviceAccountName: kbb  # Default changed to shorter name
```

#### Step 5: Update Resource Name References

If you monitor/alert on specific CronJob names:

```yaml
# OLD (v3.0.0)
{release-name}-snapshot
{release-name}-borgbackup

# NEW (v4.0.0)
kbb-{app-name}-snapshot
kbb-{app-name}-borgbackup
```

#### Example: Complete v3 to v4 Migration

See `example/values.yaml` for full Immich example in v4 format.

**For Multi-App Setup:**

```yaml
apps:
  - name: immich
    namespace: immich
    # ... immich config

  - name: nextcloud
    namespace: nextcloud
    # ... nextcloud config

  - name: gitea
    namespace: gitea
    # ... gitea config

# All three can share the same borgRepo or use different ones
```

### Technical Details

- **Helm Chart**: `kube-borg-backup` v4.0.0
- **Controller/Backup-runner**: Still v3.0.0 (NO code changes needed!)
- **Python Controllers**: Unchanged - all changes in Helm templates only
- **Kubernetes**: Still requires 1.25+ with CSI VolumeSnapshot support

### Important Notes

- **NO Python code changes** - controllers work identically with new config structure
- **Archive naming**: Automatically prefixed with app name (transparent to controllers)
- **Repository sharing**: Multiple apps can safely share borg repository (different prefixes)
- **Testing**: Existing test scripts work unchanged (configs updated to v4 structure)
- **Verbose templates OK**: Prioritized clarity over clever tricks per project conventions

## [3.0.0] - 2025-10-25

**MAJOR RELEASE** - Package renaming and release workflow improvements.

### Breaking Changes

- **GHCR Package Naming**
  - Renamed from flat naming to hierarchical structure:
    - `ghcr.io/frederikb96/kube-borg-backup-controller` → `ghcr.io/frederikb96/kube-borg-backup/controller`
    - `ghcr.io/frederikb96/kube-borg-backup-essentials` → `ghcr.io/frederikb96/kube-borg-backup/backup-runner`
  - Better organization and follows GHCR best practices
  - Old packages deprecated, all users must update Helm values

- **Component Renaming**
  - Renamed "essentials" to "backup-runner" throughout:
    - Container name: `essentials` → `backup-runner`
    - Image repository field: `essentials` → `backup-runner`
    - Directory: `apps/essentials/` → `apps/backup-runner/`
    - Workflow: `essentials-image.yaml` → `backup-runner-image.yaml`
  - More descriptive and clarifies purpose

- **Release Workflow Changes**
  - All releases now triggered by Git tags instead of push to main
  - Tag format: `v*.*.*` (e.g., `v3.0.0`)
  - Pre-release tags supported: `v*.*.*-rc*`, `v*.*.*-beta*`, `v*.*.*-alpha*`
  - Images only tagged with `:latest` on stable releases (not pre-releases)
  - Provides atomic releases of all 3 artifacts (controller + backup-runner + Helm chart)

### Changed

- **Image Tags**
  - Controller image: `ghcr.io/frederikb96/kube-borg-backup/controller:3.0.0`
  - Backup-runner image: `ghcr.io/frederikb96/kube-borg-backup/backup-runner:3.0.0`
  - Helm chart version: `3.0.0`

- **Default Values**
  - Updated `values.yaml` and `example/values.yaml` with new image repositories
  - Updated fallback image names in controller code
  - Updated all documentation references

### Migration Guide

**Helm Values Update Required:**
```yaml
# OLD (v2.0.0)
borgbackup:
  pod:
    image:
      repository: ghcr.io/frederikb96/kube-borg-backup-essentials

# NEW (v3.0.0)
borgbackup:
  pod:
    image:
      repository: ghcr.io/frederikb96/kube-borg-backup/backup-runner
```

**For GitOps Users (Flux, ArgoCD):**
- Update HelmRelease values with new image repository
- Old image tags will continue to exist but won't receive updates
- Pull the v3.0.0 images after upgrading

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

- **Controller Image**: `ghcr.io/frederikb96/kube-borg-backup/controller:2.0.0` (Python 3.13-slim, 2 entrypoints)
- **Essentials Image**: `ghcr.io/frederikb96/kube-borg-backup/backup-runner:2.0.0` (Python 3.13-alpine + borgbackup)
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
