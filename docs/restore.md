# Restore

Restores run through the `kbb` CLI from a machine with `kubectl` access to the cluster. `kbb` reads the same kubeconfig as `kubectl`, finds the application's config in the cluster, runs the configured restore hooks and starts the pods that copy the data.

## Install the CLI

```bash
# a release tag from https://github.com/frederikberg/kube-borg-backup/releases
pipx install --force "git+https://github.com/frederikberg/kube-borg-backup.git@<tag>#subdirectory=apps"

# or the current main branch
pipx install --force "git+https://github.com/frederikberg/kube-borg-backup.git#subdirectory=apps"

pipx uninstall kube-borg-backup-cli
```

Pin a tag in anything repeatable; the CLI and the images of the same release belong together.

## Addressing an application

Three global flags are required on every command and may stand before or after the subcommand:

- `-n`, `--namespace`: the namespace of the application.
- `-a`, `--app`: the application name, `apps[].name` in the Helm values.
- `-r`, `--release`: the Helm release name. The config Secrets are named `<release>-<app>-snapshot-config` and `<release>-<app>-borg-config`.

## From a snapshot

```bash
kbb -n my-app -a my-app -r my-backup snap list
kbb -n my-app -a my-app -r my-backup snap restore <snapshot name>
```

`snap list` prints the `VolumeSnapshot` objects of the application's PVCs, newest first. `snap restore` takes one of those names and:

- fails early when the snapshot is not ready or the target PVC does not exist;
- runs the restore pre-hooks;
- creates a clone PVC from the snapshot, in the storage class the backup of that PVC uses;
- starts a pod that runs `rsync --delete` from the clone into the target PVC;
- runs the post-hooks and deletes the clone PVC.

## From a Borg archive

```bash
kbb -n my-app -a my-app -r my-backup backup list
kbb -n my-app -a my-app -r my-backup backup restore <archive name>
```

`backup list` starts a short pod that reads the repository and prints the application's archives. `backup restore` takes the name from the first column and:

- picks the target PVC from the archive's prefix, or uses `--pvc`;
- runs the pre-hooks;
- starts a privileged pod that mounts the archive over FUSE and runs `rsync --delete` into the target PVC;
- runs the post-hooks only when the copy succeeded, then removes the pod and its config Secret.

## Targets and what gets replaced

- Both restores write **into an existing PVC** and make it match the source exactly: files that are not in the snapshot or archive are deleted. A restore into the live volume therefore needs the application stopped, which is what a `scale` pre-hook is for ([hooks.md](hooks.md)).
- `--pvc <name>` restores into a different PVC, for example an empty one next to the original to inspect old data. Without it, a snapshot goes back to its own source PVC.
- Neither restore has a timeout: a large archive can take hours. `Ctrl-C` or `SIGTERM` stops the pods and removes the temporary objects.
- A snapshot restore pod uses `restore.pod.image` of the chart values, an archive restore pod the Borg pod image; both default to the `backup-runner` image.
