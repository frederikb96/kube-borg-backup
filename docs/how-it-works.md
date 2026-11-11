# How it works

Each application gets two CronJobs from the chart: one creates and prunes snapshots, one backs the latest snapshots up to Borg. They share no state except the `VolumeSnapshot` objects, so either can fail, retry or be triggered by hand without the other.

## Snapshot run

- **Pre-hooks** run in the configured order. A hook with `wait: false` starts in the background (for example `pg_backup_start()` followed by a sleep and `pg_backup_stop()`), and a `startedMarker` makes the run wait until the hook has printed that marker before any snapshot is taken. See [hooks.md](hooks.md).
- **Snapshots** for all configured PVCs are created in parallel through the CSI driver and named `<pvc>-snap-<timestamp>`.
- **Post-hooks** run next, in order. They run on every exit path: after a failure, and when the pod receives `SIGTERM`.
- **Retention** keeps one snapshot per time bucket (hourly, daily, weekly, monthly) and deletes the rest. A background pre-hook that fails during the cycle discards that cycle's snapshots and skips pruning, so a snapshot taken outside backup mode never becomes a restore point.
- **Unready consumers:** a PVC whose mounting pod has been running but not ready for longer than `unreadyConsumerGraceSeconds` is not snapshotted and the run fails. Retention then keeps the last good snapshots instead of replacing them with copies of a stale volume.

## Backup run

- The controller starts creating a **clone PVC** from the latest snapshot of every PVC at once, and processes the backups one after another, because a Borg repository takes one writer at a time. The first backup starts as soon as its clone is ready while the others still provision.
- Each backup runs in its own **Borg pod**, privileged so it can read any file regardless of ownership, with the clone mounted read-only and the app's cache PVC for Borg's index when one is configured. The pod runs `borg create`, then `borg prune` with the configured retention.
- Archives are named `<prefix>-<timestamp>`; the prefix defaults to the app and backup name and can be set with `archivePrefix`. Prefixes may nest (`app-db` and `app-db-wal`), and the code matches an archive to a prefix only through the timestamp suffix, so one backup never prunes or restores another's archives.
- **Cleanup** removes the Borg pod, the clone PVC and the ephemeral config Secret after each backup. These objects are labelled and owned by the controller's Job, so deleting the Job garbage-collects what a killed controller left behind, and each run starts by deleting leftovers of earlier runs that no pod uses.
- With `snapshotted: false` no clone is created and the original PVC is mounted read-only, see [modes.md](modes.md).

## Signal handling

Both controllers handle `SIGTERM`: pods are deleted, clone PVCs and Secrets removed, and the post-hooks of a snapshot run still execute, so an eviction in the middle of a database backup window does not leave the database in backup mode.

## Components and images

- **controller** image: the Python controllers for the two CronJobs, `python -m kube_pvc_snapshot.main` and `python -m kube_snapshot_borgbackup.main`.
- **backup-runner** image: Alpine with BorgBackup, FUSE and rsync; used for backup, listing, archive restore and snapshot restore pods.
- Both are published for amd64 and arm64 to `ghcr.io/frederikberg/kube-borg-backup/`, tagged with the release version, and `latest` for stable releases.
- The chart renders the CronJobs, the config Secrets the controllers and `kbb` read, the RBAC, the optional Borg cache PVC and namespace.
