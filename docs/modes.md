# Snapshot and direct mode

Every Borg backup (`borgbackup.pvcs[]`) runs in one of two modes, chosen by `snapshotted`.

## Snapshot mode

The default. The Borg pod reads a clone of the latest snapshot, so the application keeps running and the data is a single point in time.

It needs:

- a CSI driver with `VolumeSnapshot` support and a `VolumeSnapshotClass` for it (named per PVC in `snapshot.pvcs[].snapshotClass`);
- a storage class, named per backup in `borgbackup.pvcs[].class`, that can provision a PVC from a snapshot.

To check a cluster:

```bash
kubectl get volumesnapshotclass
kubectl get storageclass <class> -o yaml
```

and to try a snapshot by hand:

```bash
kubectl create -f - <<EOF
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: test-snapshot
spec:
  source:
    persistentVolumeClaimName: <pvc>
  volumeSnapshotClassName: <snapshot class>
EOF
```

For Longhorn volumes the controller also waits, within the clone bind timeout, until the cloned volume is attached and healthy.

## Direct mode

`snapshotted: false` skips the snapshot and the clone: the Borg pod mounts the original PVC read-only. It exists for storage without snapshot support, such as NFS or host paths.

- The PVC has to be `ReadWriteMany`, or unbound while the backup runs; otherwise the Borg pod cannot mount it.
- Nothing pauses the application, so the files can change while Borg reads them. A database in direct mode is only consistent if it is stopped first, and the data of a running application is not a point-in-time copy.
- There are no local snapshots to restore from, only Borg archives.
