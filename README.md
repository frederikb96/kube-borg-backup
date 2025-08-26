# PVC CSI Snapshot + BorgBackup Helm Chart
todo...

## Helm

```sh
helm repo add mybackuprepo https://frederikb96.github.io/pvc-csi-snapshot-borgbackup-helm
helm repo update
helm search repo mybackuprepo
helm install snap mybackuprepo/snapshot-borgbackup --values my-values.yaml
```

## Test
```sh
helm template charts/snapshot-borgbackup
```
