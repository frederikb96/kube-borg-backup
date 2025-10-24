#!/bin/sh
set -euo pipefail

# Debug logging helper
DEBUG=${DEBUG:-false}
debug_log() {
    if [ "$DEBUG" = "true" ]; then
        echo "[run.sh DEBUG] $*" >&2
    fi
}

debug_log "=== Borgbackup Essential Container Starting ==="
debug_log "PID: $$"
debug_log "DEBUG mode enabled"

# Required env vars
: "${BORG_REPO?BORG_REPO env missing}"
: "${BORG_PASSPHRASE?BORG_PASSPHRASE env missing}"

debug_log "BORG_REPO: $BORG_REPO"
debug_log "BORG_PASSPHRASE: [set]"

# SSH key setup - can be provided via env var or mounted file
SSH_KEY_FILE="/root/.ssh/borg-ssh.key"

if [ -n "${SSH_PRIVATE_KEY:-}" ]; then
    debug_log "SSH_PRIVATE_KEY env var provided, writing to $SSH_KEY_FILE"
    mkdir -p /root/.ssh
    chmod 700 /root/.ssh
    echo "$SSH_PRIVATE_KEY" > "$SSH_KEY_FILE"
    chmod 600 "$SSH_KEY_FILE"
    debug_log "SSH key written from env var"
    debug_log "SSH key file size: $(wc -c < "$SSH_KEY_FILE") bytes"
    debug_log "SSH key first line: $(head -1 "$SSH_KEY_FILE")"
elif [ -f "$SSH_KEY_FILE" ]; then
    debug_log "SSH key already mounted at $SSH_KEY_FILE"
    debug_log "SSH key file size: $(wc -c < "$SSH_KEY_FILE") bytes"
else
    echo "[run.sh] ERROR: No SSH key provided (neither SSH_PRIVATE_KEY env var nor mounted file at $SSH_KEY_FILE)" >&2
    exit 1
fi

# SSH configuration for BorgBackup
# IdentitiesOnly=yes is CRITICAL with multiple keys (prevents "too many authentication failures")
# StrictHostKeyChecking=no allows connecting to new hosts without manual verification
export BORG_RSH="ssh -o IdentityFile=$SSH_KEY_FILE -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
debug_log "BORG_RSH: $BORG_RSH"

PREFIX=${BORG_PREFIX:-backup}
DIR=${BACKUP_DIR:-/data}
LOCK_WAIT=${BORG_LOCK_WAIT:-600}

debug_log "PREFIX: $PREFIX"
debug_log "DIR: $DIR"
debug_log "LOCK_WAIT: ${LOCK_WAIT}s"

# Build retention flags from env vars
KEEP_FLAGS=""
[ -n "${BORG_KEEP_HOURLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-hourly=$BORG_KEEP_HOURLY"
[ -n "${BORG_KEEP_DAILY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-daily=$BORG_KEEP_DAILY"
[ -n "${BORG_KEEP_WEEKLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-weekly=$BORG_KEEP_WEEKLY"
[ -n "${BORG_KEEP_MONTHLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-monthly=$BORG_KEEP_MONTHLY"
[ -n "${BORG_KEEP_YEARLY:-}" ] && KEEP_FLAGS="$KEEP_FLAGS --keep-yearly=$BORG_KEEP_YEARLY"

if [ -n "$KEEP_FLAGS" ]; then
    debug_log "Retention flags:$KEEP_FLAGS"
else
    debug_log "No retention policy specified"
fi

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
debug_log "Checking repo status with 'borg info'..."
OUTPUT=$(borg info "$BORG_REPO" 2>&1)
EXIT_CODE=$?
debug_log "borg info exit code: $EXIT_CODE"
debug_log "borg info output (first 200 chars): ${OUTPUT:0:200}"

# Exit 0 OR (Exit 2 AND locked) -> proceed
if [ $EXIT_CODE -eq 0 ]; then
    echo "[run.sh] Repo ready"
    debug_log "Repository is initialized and accessible"
elif [ $EXIT_CODE -eq 2 ] && echo "$OUTPUT" | grep -q "Failed to create/acquire the lock"; then
    echo "[run.sh] Repo locked, will wait during backup"
    debug_log "Repository is locked by another process, borg create will wait"
elif [ $EXIT_CODE -eq 2 ] && echo "$OUTPUT" | grep -q "is not a valid repository"; then
    echo "[run.sh] Repo not initialized, initializing..."
    debug_log "Initializing repository with repokey-blake2 encryption..."
    borg init --encryption repokey-blake2 "$BORG_REPO"
    debug_log "Repository initialized successfully"
else
    # All other cases: fail
    echo "[run.sh] ERROR: Unexpected failure" >&2
    echo "$OUTPUT" >&2
    debug_log "FATAL: borg info failed with unexpected exit code $EXIT_CODE"
    exit 1
fi

# Archive name with timezone-aware timestamp
ARCHIVE="${PREFIX}-$(date -u +%Y-%m-%d-%H-%M-%S)"
debug_log "Archive name: $ARCHIVE"
debug_log "Backup directory: $DIR"

# Start borg create in background and capture PID
echo "[run.sh] Starting borg create for archive: $ARCHIVE"
debug_log "borg create command: borg create --lock-wait $LOCK_WAIT --list --filter=AME --stats --files-cache mtime,size $BORG_REPO::$ARCHIVE $DIR"
borg create --lock-wait "$LOCK_WAIT" --list --filter=AME --stats --files-cache mtime,size "$BORG_REPO::$ARCHIVE" "$DIR" &
BORG_PID=$!
echo "[run.sh] Borg PID: $BORG_PID"
debug_log "Waiting for borg process to complete..."

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
