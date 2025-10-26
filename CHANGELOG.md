# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [5.0.5] - 2025-10-26

### Added

- **Dual-Thread Real-Time Streaming** (controller v5.0.5)
  - Implemented concurrent log and event streaming for borg backup pods
  - Logs stream with `[pod-name]` prefix, events with `[EVENT]` prefix
  - Real-time visibility into pod execution, image pulls, volume attachment, container lifecycle

### Fixed

- **Log Streaming "Bad Request" Errors** (controller v5.0.5)
  - Fixed premature log streaming that occurred before container logs were ready
  - Now waits for `container.state.running.started_at` before streaming
  - Single `follow=True` call captures complete log output from start to finish
  - Eliminated HTTP 400 errors and log loss on fast-completing pods

- **Event Streaming Duplicates** (controller v5.0.5)
  - Fixed duplicate events replaying after 60-second watch timeout
  - Implemented `resourceVersion` tracking across reconnections
  - Events now stream continuously without duplicates
  - Seamless reconnection handling with no visible interruption

### Improved

- **Clean Output Format** (controller v5.0.5)
  - Removed verbose "waiting for logs" and "reconnecting" messages
  - Professional, production-ready log format
  - Clean interleaved event and log streams

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
