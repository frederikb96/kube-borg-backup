#!/bin/sh
set -euo pipefail

: "${BORG_REPO?BORG_REPO env missing}"
: "${BORG_PASSPHRASE?BORG_PASSPHRASE env missing}"

PREFIX=${BORG_PREFIX:-backup}
FLAGS=${BORG_FLAGS:-}
DIR=${BACKUP_DIR:-/data}

borg break-lock "$BORG_REPO" || true
if ! borg info "$BORG_REPO" >/dev/null 2>&1; then
  borg init --encryption repokey-blake2 "$BORG_REPO"
fi

ARCHIVE="${PREFIX}-$(date +%Y-%m-%d-%H-%M)"

borg create --list --filter=AME --stats --files-cache mtime,size "$BORG_REPO::$ARCHIVE" "$DIR"
borg prune -v --list $FLAGS --glob-archives="${PREFIX}-*" "$BORG_REPO"
