"""Archive naming contract, shared by the backup runner, the controller and the CLI.

An archive is named ``{prefix}-{timestamp}``. Prefixes nest: a CNPG app configures one backup
under ``<app>-db`` and another under ``<app>-db-wal``, so ``<app>-db`` is a prefix of every
``<app>-db-wal`` archive name. Any pattern or test that selects one prefix's archives therefore
has to anchor on the timestamp — a bare ``{prefix}-*`` or ``str.startswith`` selects the sibling's
archives too, which makes a prune delete them and a restore write them to the wrong PVC.
"""

import re
from datetime import UTC, datetime

TIMESTAMP_FORMAT = '%Y-%m-%d-%H-%M-%S'

# borg --glob-archives takes fnmatch patterns, where '?' is exactly one character.
_TIMESTAMP_GLOB = '????-??-??-??-??-??'
_TIMESTAMP_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}')


def archive_name(prefix: str) -> str:
    """Build the archive name for a backup taken now."""
    return f"{prefix}-{datetime.now(UTC).strftime(TIMESTAMP_FORMAT)}"


def archive_glob(prefix: str) -> str:
    """Build a borg --glob-archives pattern matching this prefix's archives and no sibling's."""
    return f"{prefix}-{_TIMESTAMP_GLOB}"


def archive_matches(archive: str, prefix: str) -> bool:
    """Report whether an archive name belongs to this prefix rather than to a longer one."""
    if not archive.startswith(f"{prefix}-"):
        return False
    return _TIMESTAMP_PATTERN.fullmatch(archive[len(prefix) + 1:]) is not None
