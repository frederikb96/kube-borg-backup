#!/bin/sh
set -euo pipefail

: "${BORG_REPO?BORG_REPO env missing}"
: "${BORG_PASSPHRASE?BORG_PASSPHRASE env missing}"

# SSH configuration for BorgBackup
# Key mounted via Kubernetes secret at /root/.ssh/borg-ssh.key
# IdentitiesOnly=yes is CRITICAL with multiple keys (prevents "too many authentication failures")
# StrictHostKeyChecking=no allows connecting to new hosts without manual verification
export BORG_RSH="ssh -o IdentityFile=/root/.ssh/borg-ssh.key -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"

PREFIX=${BORG_PREFIX:-backup}
DIR=${BACKUP_DIR:-/data}
LOCK_WAIT=${BORG_LOCK_WAIT:-600}

# Build retention flags from env vars
KEEP_FLAGS=""
[ -n "${BORG_KEEP_HOURLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-hourly=$BORG_KEEP_HOURLY"
[ -n "${BORG_KEEP_DAILY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-daily=$BORG_KEEP_DAILY"
[ -n "${BORG_KEEP_WEEKLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-weekly=$BORG_KEEP_WEEKLY"
[ -n "${BORG_KEEP_MONTHLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-monthly=$BORG_KEEP_MONTHLY"
[ -n "${BORG_KEEP_YEARLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-yearly=$BORG_KEEP_YEARLY"

# Track borg PID for signal handling
BORG_PID=""

# SIGTERM/SIGINT trap handler
handle_signal() {
    echo "[run.sh] Received termination signal, stopping borg gracefully..."
    if [ -n "$BORG_PID" ] && kill -0 "$BORG_PID" 2>/dev/null; then
        # Send SIGINT to borg (triggers checkpoint creation, then abort)
        echo "[run.sh] Sending SIGINT to borg PID $BORG_PID (checkpoint + abort)..."
        kill -INT "$BORG_PID" 2>/dev/null || true

        # Wait up to 20 seconds for borg to finish checkpoint and exit
        echo "[run.sh] Waiting up to 20 seconds for checkpoint to complete..."
        for i in $(seq 1 20); do
            if ! kill -0 "$BORG_PID" 2>/dev/null; then
                echo "[run.sh] Borg stopped gracefully after ${i}s"
                exit 143
            fi
            sleep 1
        done

        # Still running after 20s - force kill and cleanup lock
        if kill -0 "$BORG_PID" 2>/dev/null; then
            echo "[run.sh] Checkpoint not complete after 20s, forcing termination..."
            kill -9 "$BORG_PID" 2>/dev/null || true
            sleep 1
            echo "[run.sh] Borg killed with SIGKILL"

            # Cleanup lock manually
            echo "[run.sh] Breaking stale lock..."
            borg break-lock "$BORG_REPO" 2>&1 || echo "[run.sh] Failed to break lock (may not exist)"
            echo "[run.sh] Lock cleanup complete"
        fi
    fi
    exit 143  # Standard exit code for SIGTERM
}

# Set up trap for SIGTERM and SIGINT
trap 'handle_signal' TERM INT

echo "[run.sh] Starting backup: $PREFIX"
echo "[run.sh] Lock wait timeout: ${LOCK_WAIT}s"
echo "[run.sh] PID: $$"

# Check repo initialization
OUTPUT=$(borg info "$BORG_REPO" 2>&1)
EXIT_CODE=$?

# Exit 0 OR (Exit 2 AND locked) -> proceed
if [ $EXIT_CODE -eq 0 ]; then
    echo "[run.sh] Repo ready"
elif [ $EXIT_CODE -eq 2 ] && echo "$OUTPUT" | grep -q "Failed to create/acquire the lock"; then
    echo "[run.sh] Repo locked, will wait during backup"
elif [ $EXIT_CODE -eq 2 ] && echo "$OUTPUT" | grep -q "is not a valid repository"; then
    echo "[run.sh] Repo not initialized, initializing..."
    borg init --encryption repokey-blake2 "$BORG_REPO"
else
    # All other cases: fail
    echo "[run.sh] ERROR: Unexpected failure" >&2
    echo "$OUTPUT" >&2
    exit 1
fi

# Archive name with timezone-aware timestamp
ARCHIVE="${PREFIX}-$(date -u +%Y-%m-%d-%H-%M-%S)"

# Start borg create in background and capture PID
echo "[run.sh] Starting borg create for archive: $ARCHIVE"
borg create --lock-wait "$LOCK_WAIT" --list --filter=AME --stats --files-cache mtime,size "$BORG_REPO::$ARCHIVE" "$DIR" &
BORG_PID=$!
echo "[run.sh] Borg PID: $BORG_PID"

# Wait for borg to complete (or be interrupted by signal)
if wait "$BORG_PID"; then
    echo "[run.sh] Backup complete: $ARCHIVE"
else
    EXIT_CODE=$?
    echo "[run.sh] Borg exited with code: $EXIT_CODE"
    exit $EXIT_CODE
fi

# Prune old archives if retention flags provided
if [ -n "$KEEP_FLAGS" ]; then
    echo "[run.sh] Pruning old archives with retention policy..."
    echo "[run.sh] Retention flags: $KEEP_FLAGS"
    # shellcheck disable=SC2086
    borg prune --lock-wait "$LOCK_WAIT" -v --list $KEEP_FLAGS --glob-archives="${PREFIX}-*" "$BORG_REPO"
    echo "[run.sh] Prune complete"
else
    echo "[run.sh] No retention policy specified, skipping prune"
fi

echo "[run.sh] Backup successful!"
