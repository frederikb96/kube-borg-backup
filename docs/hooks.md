# Hooks

Hooks run commands or scale workloads at fixed points. Snapshot hooks keep a database consistent while the snapshots are taken; restore hooks stop and start an application around a restore. The chart's [values.yaml](../charts/kube-borg-backup/values.yaml) holds the full commented syntax and [example/values.yaml](../example/values.yaml) a PostgreSQL case.

## Snapshot hooks

Set per PVC under `snapshot.pvcs[].hooks`, as `pre` and `post` lists of exec hooks (`pod`, optional `container`, `command`).

- Hooks run sequentially in the listed order.
- `wait: true` (the default) blocks until the command ends, which is how a hook delays the snapshots, for example a `sleep` after a checkpoint.
- `wait: false` starts the command in the background and continues. The snapshots are taken while it runs, which fits `pg_backup_start()` followed by a sleep and `pg_backup_stop()` in one `psql` session.
- `startedMarker` with `startedTimeoutSeconds` on a background hook makes the run wait until the command has printed the marker to stdout. A hook that fails, ends without the marker or stays silent for the timeout aborts the run before any snapshot. Without a marker the snapshots start immediately.
- A background pre-hook that fails later in the cycle discards that cycle's snapshots and skips pruning.
- Post-hooks run on every exit path, including a failed run and `SIGTERM`.

## Restore hooks

Set per application under `restore.preHooks` and `restore.postHooks`; `kbb` runs them from the machine it is started on.

- **Pre-hooks are fail-fast:** the first failure stops the restore before anything is changed.
- **Post-hooks run only after a successful restore**, so a failed restore does not scale a broken application back up. A failing post-hook is reported as a warning and does not fail the restore.
- Hook types:
  - `exec`: run a command in a pod (`pod`, optional `container`, `command`).
  - `scale`: set the replicas of a `deployment` or `statefulset`.
  - `shell`: run a local command on the machine running `kbb` (`command`, optional `timeout`, 300 seconds by default). Restore only.
- Consecutive hooks marked `parallel: true` run concurrently; the others run one after another.
- `restore.rbac.additionalRules` in the chart values extends the restore RBAC for hooks that touch other API groups.
