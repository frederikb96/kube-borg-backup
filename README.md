# PVC CSI Snapshot + BorgBackup Helm Chart
This Helm chart deploys a Kubernetes CronJob that creates snapshots of Persistent Volume Claims (PVCs) using the CSI snapshot feature and backs them up using BorgBackup to a remote repository.

still in work... 2025.08.27

## Quickstart

```sh
helm repo add snapshot-backup https://frederikb96.github.io/pvc-csi-snapshot-borgbackup-helm
helm repo update
helm search repo snapshot-backup
helm show values snapshot-backup/snapshot-borgbackup > my-values.yaml
# Edit my-values.yaml to your needs
helm install snap snapshot-backup/snapshot-borgbackup --values my-values.yaml
```

## Test
```sh
helm template charts/snapshot-borgbackup
```
