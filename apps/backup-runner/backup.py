#!/usr/bin/env python3
"""Borgbackup backup operation.

This script orchestrates BorgBackup backup operations:
1. Reads configuration from mounted config file
2. Sets up SSH authentication
3. Creates backup archive (directly, no pre-check)
4. Applies retention policy
5. Handles graceful shutdown on SIGTERM/SIGINT

If backup fails with exit code 2 (typically uninitialized repo),
falls back to checking/initializing repository and retries.
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, UTC

import psutil

from common import load_config, setup_ssh_key, get_borg_env, init_borg_repo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Global state for signal handling
_borg_process: subprocess.Popen | None = None
_borg_repo: str | None = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Borgbackup backup operation")
    parser.add_argument(
        "-c", "--config",
        default="/config/config.yaml",
        help="Path to config file (default: /config/config.yaml)"
    )
    return parser.parse_args()


def validate_backup_config(config: dict) -> None:
    """Validate backup-specific required fields.

    Args:
        config: Configuration dictionary

    Raises:
        SystemExit: If backup-specific fields are missing
    """
    required = ['prefix', 'backupDir', 'lockWait']
    missing = [field for field in required if field not in config]
    if missing:
        logger.error(f"Config missing backup-specific fields: {', '.join(missing)}")
        sys.exit(1)


def handle_shutdown(signum, frame):
    """Signal handler for graceful shutdown.

    When SIGTERM/SIGINT is received:
    1. Send SIGINT to borg process (triggers checkpoint)
    2. Wait up to 20 seconds for borg to finish
    3. If still running, send SIGKILL and break lock

    Args:
        signum: Signal number
        frame: Current stack frame
    """
    global _borg_process, _borg_repo

    logger.info("Received termination signal, stopping borg gracefully...")

    if _borg_process and _borg_process.poll() is None:
        logger.info(f"Sending SIGINT to borg PID {_borg_process.pid} (checkpoint + abort)...")

        try:
            _borg_process.send_signal(signal.SIGINT)
        except Exception as exc:
            logger.warning(f"Failed to send SIGINT: {exc}")

        # Wait up to 20 seconds for checkpoint
        logger.info("Waiting up to 20 seconds for checkpoint to complete...")
        for i in range(1, 21):
            if _borg_process.poll() is not None:
                logger.info(f"Borg stopped gracefully after {i}s")
                sys.exit(143)
            time.sleep(1)

        # Still running after 20s - force kill and cleanup
        if _borg_process.poll() is None:
            logger.info("Checkpoint not complete after 20s, forcing termination...")
            try:
                _borg_process.kill()
                _borg_process.wait(timeout=1)
                logger.info("Borg killed with SIGKILL")
            except Exception as exc:
                logger.warning(f"Failed to kill borg: {exc}")

            # Cleanup lock manually
            if _borg_repo:
                logger.info("Breaking stale lock...")
                try:
                    subprocess.run(
                        ['borg', 'break-lock', _borg_repo],
                        timeout=10,
                        capture_output=True
                    )
                    logger.info("Lock cleanup complete")
                except Exception as exc:
                    logger.warning(f"Failed to break lock: {exc}")

    sys.exit(143)


def monitor_borg_heartbeat(pid: int, stop_event: threading.Event) -> None:
    """Monitor borg process and print heartbeat every 60s.

    Tracks CPU time, I/O bytes, memory usage, and thread count to provide
    progress indication during silent deduplication phases.

    Args:
        pid: Borg process ID to monitor
        stop_event: Event to signal monitoring should stop
    """
    try:
        process = psutil.Process(pid)

        # Establish baseline metrics
        baseline_cpu = process.cpu_times()
        baseline_io = process.io_counters()
        baseline_threads = process.num_threads()
        baseline_memory = process.memory_info().rss / (1024 * 1024)  # MB

        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
        logger.info(
            f"[{timestamp}] [HEARTBEAT] Baseline established | "
            f"Threads: {baseline_threads} | Memory: {baseline_memory:.1f}MB"
        )

        # Store previous values for delta calculation
        prev_cpu = baseline_cpu
        prev_io = baseline_io

        while not stop_event.wait(timeout=60):
            try:
                # Check if process still exists
                if not process.is_running():
                    break

                # Get current metrics
                current_cpu = process.cpu_times()
                current_io = process.io_counters()
                current_memory = process.memory_info().rss / (1024 * 1024)  # MB

                # Calculate deltas since last heartbeat
                cpu_delta = (current_cpu.user - prev_cpu.user) + (current_cpu.system - prev_cpu.system)
                io_delta = (current_io.read_bytes - prev_io.read_bytes) + (current_io.write_bytes - prev_io.write_bytes)
                io_delta_mb = io_delta / (1024 * 1024)  # MB

                # Update previous values
                prev_cpu = current_cpu
                prev_io = current_io

                timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
                logger.info(
                    f"[{timestamp}] [HEARTBEAT] ✓ ACTIVE | CPU: +{cpu_delta:.1f}s | "
                    f"I/O: +{io_delta_mb:.1f}MB | Memory: {current_memory:.1f}MB"
                )

            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Process terminated or access denied - normal exit
                break
            except Exception as exc:
                logger.warning(f"Heartbeat monitoring error: {exc}")
                break

    except Exception as exc:
        logger.warning(f"Failed to start heartbeat monitoring: {exc}")


def run_backup(config: dict) -> int:
    """Run borg backup with configuration.

    Args:
        config: Configuration dictionary

    Returns:
        Exit code (0 = success)
    """
    global _borg_process, _borg_repo

    # Validate backup-specific fields
    validate_backup_config(config)

    # Extract config
    borg_repo = config['borgRepo']
    prefix = config['prefix']
    backup_dir = config['backupDir']
    lock_wait = config['lockWait']
    retention = config.get('retention', {})

    _borg_repo = borg_repo

    # Setup SSH and environment using common functions
    ssh_key_file = setup_ssh_key(config['sshPrivateKey'])
    env = get_borg_env(config, ssh_key_file)

    logger.info(f"Starting backup: {prefix}")
    logger.info(f"Lock wait timeout: {lock_wait}s")
    logger.info(f"PID: {os.getpid()}")

    # Build archive name with UTC timestamp
    archive_name = f"{prefix}-{datetime.now(UTC).strftime('%Y-%m-%d-%H-%M-%S')}"
    archive = f"{borg_repo}::{archive_name}"

    logger.info(f"Creating archive: {archive_name}")
    logger.info(f"Backup directory: {backup_dir}")

    # Build borg create command
    borg_create_cmd = [
        'borg', 'create',
        '--lock-wait', str(lock_wait),
        '--list',
        '--filter=AME',
        '--stats',
        '--files-cache', 'mtime,size',
        archive,
        backup_dir
    ]

    # Start borg create process
    try:
        _borg_process = subprocess.Popen(
            borg_create_cmd,
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr
        )

        logger.info(f"Borg PID: {_borg_process.pid}")

        # Start heartbeat monitoring thread
        stop_heartbeat = threading.Event()
        heartbeat_thread = threading.Thread(
            target=monitor_borg_heartbeat,
            args=(_borg_process.pid, stop_heartbeat),
            name="heartbeat-monitor",
            daemon=True
        )
        heartbeat_thread.start()

        # Wait for borg to complete
        exit_code = _borg_process.wait()
        _borg_process = None

        # Stop heartbeat monitoring
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=2)

        # If exit code 2, check repo status and retry
        if exit_code == 2:
            logger.info("Borg create failed with exit code 2, checking repository status...")
            init_borg_repo(config, env)

            # Retry borg create after repo check/init
            logger.info("Retrying backup after repository check...")
            _borg_process = subprocess.Popen(
                borg_create_cmd,
                env=env,
                stdout=sys.stdout,
                stderr=sys.stderr
            )

            logger.info(f"Borg PID: {_borg_process.pid}")

            # Restart heartbeat monitoring for retry
            stop_heartbeat = threading.Event()
            heartbeat_thread = threading.Thread(
                target=monitor_borg_heartbeat,
                args=(_borg_process.pid, stop_heartbeat),
                name="heartbeat-monitor-retry",
                daemon=True
            )
            heartbeat_thread.start()

            exit_code = _borg_process.wait()
            _borg_process = None

            # Stop heartbeat monitoring
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=2)

        if exit_code != 0:
            logger.error(f"Borg exited with code: {exit_code}")
            return exit_code

        logger.info(f"Backup complete: {archive_name}")

    except Exception as exc:
        logger.error(f"Backup failed: {exc}")
        return 1

    # Apply retention policy if specified
    if retention:
        logger.info("Pruning old archives with retention policy...")
        logger.info(f"Retention: {retention}")

        # Build prune command
        prune_cmd = ['borg', 'prune', '--lock-wait', str(lock_wait), '-v', '--list']

        if retention.get('hourly'):
            prune_cmd.extend(['--keep-hourly', str(retention['hourly'])])
        if retention.get('daily'):
            prune_cmd.extend(['--keep-daily', str(retention['daily'])])
        if retention.get('weekly'):
            prune_cmd.extend(['--keep-weekly', str(retention['weekly'])])
        if retention.get('monthly'):
            prune_cmd.extend(['--keep-monthly', str(retention['monthly'])])
        if retention.get('yearly'):
            prune_cmd.extend(['--keep-yearly', str(retention['yearly'])])

        prune_cmd.extend(['--glob-archives', f'{prefix}-*', borg_repo])

        try:
            result = subprocess.run(
                prune_cmd,
                env=env,
                capture_output=False,
                timeout=lock_wait
            )

            if result.returncode != 0:
                logger.error(f"Prune failed with exit code: {result.returncode}")
                return result.returncode

            logger.info("Prune complete")

        except subprocess.TimeoutExpired:
            logger.error(f"Prune timed out after {lock_wait}s")
            return 1
        except Exception as exc:
            logger.error(f"Prune failed: {exc}")
            return 1
    else:
        logger.info("No retention policy specified, skipping prune")

    logger.info("Backup successful!")
    return 0


def main() -> int:
    """Main entry point."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Parse args and load config
    args = parse_args()
    config = load_config(args.config)

    # Run backup
    return run_backup(config)


if __name__ == '__main__':
    sys.exit(main())
