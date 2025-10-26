# kube-borg-backup CLI

Command-line tool for restore operations with kube-borg-backup.

## Installation

Install via pipx from GitHub:

```bash
# Latest from main branch
pipx install git+https://github.com/frederikb96/kube-borg-backup.git#subdirectory=apps/cli

# Specific version
pipx install git+https://github.com/frederikb96/kube-borg-backup.git@v6.0.0#subdirectory=apps/cli
```

## Usage

```bash
# List snapshots
kbb -n <namespace> -a <app> snap list

# Restore from snapshot
kbb -n <namespace> -a <app> snap restore <snapshot-id>
kbb -n <namespace> -a <app> snap restore <snapshot-id> --pvc <target-pvc>

# List borg archives
kbb -n <namespace> -a <app> backup list

# Restore from borg archive
kbb -n <namespace> -a <app> backup restore <archive-id>
kbb -n <namespace> -a <app> backup restore <archive-id> --pvc <target-pvc>
```

## Development

```bash
cd apps/cli
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# Test commands
kbb -n test-namespace -a test-app snap list
```
