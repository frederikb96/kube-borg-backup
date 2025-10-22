# kube-borg-backup - Agent Instructions

## Project Overview

This repository contains `kube-borg-backup`, a Kubernetes tool for automating PVC snapshots via CSI and backing them up to BorgBackup repositories. The project consists of:

1. **apps/controller/** - Python application handling snapshot creation/pruning and backup orchestration
2. **apps/essentials/** - Minimal Borg container for executing backups
3. **charts/kube-borg-backup/** - Helm chart packaging both CronJobs

## Development & Testing

**IMPORTANT:** Testing should be done locally using direct Helm installation, NOT through the full release cycle.

### Local Testing Workflow

See README.md "Development" section for full instructions. Summary:

```bash
# Lint chart
helm lint charts/kube-borg-backup

# Test render
helm template test charts/kube-borg-backup --values example/values.yaml

# Install directly to test namespace
helm upgrade --install kube-borg-backup-dev charts/kube-borg-backup \
  --namespace kube-borg-backup-dev \
  --create-namespace \
  --values example/values.yaml
```

### Test Namespace

**ALL TESTING** must be done in the dedicated test namespace: `kube-borg-backup-dev`

- Create small test PVCs (1-5Gi)
- Use `-dev` suffix for helm release names
- Never modify production namespaces during testing
- Clean up test resources after validation

## Python Application Structure

**Unified controller image** (`apps/controller/`):
- Contains both `kube_pvc_snapshot` and `kube_snapshot_borgbackup` modules
- Shared dependencies in single requirements.txt
- Different entrypoints for each CronJob:
  - Snapshot: `python -m kube_pvc_snapshot.main`
  - Backup: `python -m kube_snapshot_borgbackup.main`

**Essentials image** (`apps/essentials/`):
- Minimal Alpine + Borg + SSH
- Used by dynamically spawned backup pods
- No Python dependencies

## Helm Chart

- **Single chart, multiple CronJobs** - snapshot and backup are separate but deployed together
- **Version synchronization** - chart version matches app version (single version for whole project)
- **Image references** - values.yaml points to `ghcr.io/frederikb96/kube-borg-backup-*` images

## Code Style & Conventions

- **Python:** PEP 8 formatting, PEP 257 docstrings, type hints on public functions
- **Error handling:** Simple and explicit, print to stderr, exit non-zero on failure
- **KISS principle:** Prefer pragmatism and clarity over premature architecture
- **Documentation:** Update README.md, chart README, and CHANGELOG.md when making changes

## GitHub Workflows

Three CI/CD workflows in `.github/workflows/`:
1. **release.yaml** - Packages and publishes Helm chart to gh-pages
2. **controller-image.yaml** - Builds and pushes controller image
3. **essentials-image.yaml** - Builds and pushes essentials image

Workflows trigger on changes to relevant paths in main branch.

## Repository Structure

```
kube-borg-backup/
├── apps/
│   ├── controller/         # Python snapshot + backup controller
│   └── essentials/         # Borg backup runner
├── charts/
│   └── kube-borg-backup/   # Helm chart
├── example/                # Example values for Immich
├── .github/workflows/      # CI/CD automation
├── CHANGELOG.md            # Version history
├── CLAUDE.md               # This file
├── LICENSE                 # MIT license
└── README.md               # User documentation
```

## Safety & Best Practices

- **Read before write:** Always read existing files before modifying them
- **Test thoroughly:** Validate changes in test namespace before proposing production use
- **Document changes:** Update CHANGELOG.md and relevant READMEs
- **Version consistency:** Keep chart version, app VERSION files, and CHANGELOG in sync
- **No secrets in code:** All sensitive data via Kubernetes secrets

## References

- **Kubernetes client library:** https://github.com/kubernetes-client/python
- **Helm best practices:** https://helm.sh/docs/chart_best_practices/
- **BorgBackup docs:** https://borgbackup.readthedocs.io/

## Agent-Specific Notes

- This CLAUDE.md is public and generic - no user-specific information
- For project-specific context, development logs, and detailed plans, refer to Obsidian notes
- When uncertain about approach or architecture, ask the user before implementing
