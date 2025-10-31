"""Backup runner module for kube-borg-backup.

This module contains shared utilities used by backup operations:
- common.py: Borg repo initialization, SSH keys, config loading
- hooks.py: Hook execution system (exec, scale, shell)
- backup.py: Main backup execution script
- list.py: List borg archives
- restore.py: Restore from borg archives
"""

__version__ = "3.0.0"

__all__ = ['common', 'hooks']
