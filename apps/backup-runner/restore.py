#!/usr/bin/env python3
"""Borgbackup restore operation via FUSE mount and rsync.

This script orchestrates BorgBackup restore operations:
1. Reads configuration from mounted config file
2. Validates archiveName field (required for restore)
3. Sets up SSH authentication
4. Mounts borg archive via FUSE in background thread
5. Runs rsync to copy data from mount to target PVC
6. Unmounts and cleans up on completion or signal
"""

import argparse
import logging
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from common import load_config, setup_ssh_key, get_borg_env, init_borg_repo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Global state for cleanup
_mount_point: Path | None = None
_borg_process: subprocess.Popen | None = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Borgbackup restore operation")
    parser.add_argument(
        "-c", "--config",
        default="/config/config.yaml",
        help="Path to config file (default: /config/config.yaml)"
    )
    return parser.parse_args()


def validate_restore_config(config: dict) -> None:
    """Validate restore-specific required fields.

    Args:
        config: Configuration dictionary

    Raises:
        SystemExit: If restore-specific fields are missing
    """
    required = ['archiveName']
    missing = [field for field in required if field not in config]
    if missing:
        logger.error(f"Config missing restore-specific fields: {', '.join(missing)}")
        sys.exit(1)


def mount_archive_background(archive: str, mount_point: Path, env: dict) -> None:
    """Mount borg archive in background thread using FUSE.

    Runs 'borg mount -f' in foreground mode (required by FUSE).
    Thread will block until borg exits.

    Args:
        archive: Full archive specification (repo::archive)
        mount_point: Directory to mount archive at
        env: Environment variables dict with BORG_* variables
    """
    global _borg_process

    cmd = ['borg', 'mount', '-f', archive, str(mount_point)]

    logger.info(f"Starting FUSE mount: {' '.join(cmd)}")

    try:
        _borg_process = subprocess.Popen(
            cmd,
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr
        )

        logger.info(f"FUSE mount process started (PID: {_borg_process.pid})")

        # Wait for borg mount to exit (happens on unmount or error)
        exit_code = _borg_process.wait()

        if exit_code != 0:
            logger.error(f"FUSE mount exited with code {exit_code}")
        else:
            logger.info("FUSE mount exited successfully")

    except Exception as exc:
        logger.error(f"FUSE mount failed: {exc}")


def wait_for_mount_ready(mount_point: Path, timeout: int = 30) -> bool:
    """Poll until FUSE mount is ready.

    Borg creates special files/directories when mount is ready.
    We check for the mount point to be populated.

    Args:
        mount_point: Directory where archive should be mounted
        timeout: Maximum seconds to wait

    Returns:
        True if mount ready, False if timeout
    """
    logger.info(f"Waiting for mount to be ready (timeout: {timeout}s)...")

    start_time = time.time()

    while time.time() - start_time < timeout:
        try:
            # Check if mount point has any content
            # Borg mount typically shows archive contents at root
            if any(mount_point.iterdir()):
                logger.info(f"Mount ready after {int(time.time() - start_time)}s")
                return True
        except Exception:
            # Mount point might not be accessible yet
            pass

        time.sleep(0.5)

    logger.error(f"Mount not ready after {timeout}s timeout")
    return False


def run_rsync(source: Path, target: Path) -> int:
    """Run rsync to copy data from source to target.

    Uses rsync with --delete to ensure target matches source exactly.

    Args:
        source: Source directory (mounted archive)
        target: Target directory (PVC to restore to)

    Returns:
        Exit code from rsync (0 = success)

    Raises:
        subprocess.CalledProcessError: If rsync fails
    """
    # Ensure trailing slashes for rsync directory sync behavior
    source_path = f"{source}/"
    target_path = f"{target}/"

    logger.info(f"Starting rsync: {source_path} -> {target_path}")

    cmd = [
        'rsync',
        '-av',           # Archive mode, verbose
        '--delete',      # Delete files in target not in source
        source_path,
        target_path
    ]

    try:
        result = subprocess.run(
            cmd,
            stdout=sys.stdout,
            stderr=sys.stderr,
            check=True
        )

        logger.info("Rsync completed successfully")
        return result.returncode

    except subprocess.CalledProcessError as exc:
        logger.error(f"Rsync failed with exit code {exc.returncode}")
        raise


def cleanup(signum=None, frame=None) -> None:
    """Unmount archive and cleanup resources.

    Called on normal exit or when receiving SIGTERM/SIGINT.

    Args:
        signum: Signal number (if called from signal handler)
        frame: Current stack frame (if called from signal handler)
    """
    global _mount_point, _borg_process

    logger.info("Cleaning up...")

    # Unmount if mount point exists and is mounted
    if _mount_point and _mount_point.exists():
        try:
            logger.info(f"Unmounting {_mount_point}...")

            # Use fusermount to unmount FUSE filesystem
            result = subprocess.run(
                ['fusermount', '-u', str(_mount_point)],
                timeout=10,
                capture_output=True,
                text=True
            )

            if result.returncode == 0:
                logger.info("Unmount successful")
            else:
                logger.warning(f"Unmount failed: {result.stderr}")

        except subprocess.TimeoutExpired:
            logger.error("Unmount timed out after 10s")
        except Exception as exc:
            logger.warning(f"Unmount error: {exc}")

    # Wait for borg process to exit (if still running)
    if _borg_process and _borg_process.poll() is None:
        logger.info("Waiting for FUSE mount process to exit...")

        try:
            _borg_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("FUSE mount process did not exit, killing...")
            _borg_process.kill()

    # Exit with appropriate code
    if signum:
        logger.info(f"Exiting due to signal {signum}")
        sys.exit(143)  # 128 + 15 (SIGTERM)


def run_restore(config: dict) -> int:
    """Run borg restore with configuration.

    Args:
        config: Configuration dictionary

    Returns:
        Exit code (0 = success)
    """
    global _mount_point

    # Validate restore-specific fields
    validate_restore_config(config)

    # Extract config
    borg_repo = config['borgRepo']
    archive_name = config['archiveName']
    target_path = Path(config.get('targetPath', '/target'))

    # Setup SSH and environment
    ssh_key_file = setup_ssh_key(config['sshPrivateKey'])
    env = get_borg_env(config, ssh_key_file)

    # Check repository status
    init_borg_repo(config, env)

    logger.info("Starting restore operation")
    logger.info(f"Archive: {archive_name}")
    logger.info(f"Target: {target_path}")

    # Build full archive specification
    archive = f"{borg_repo}::{archive_name}"

    # Create mount point
    mount_point = Path('/source')
    mount_point.mkdir(parents=True, exist_ok=True)
    _mount_point = mount_point

    # Start FUSE mount in background thread
    logger.info("Mounting archive via FUSE...")

    mount_thread = threading.Thread(
        target=mount_archive_background,
        args=(archive, mount_point, env),
        daemon=True
    )

    mount_thread.start()

    # Wait for mount to be ready
    if not wait_for_mount_ready(mount_point):
        logger.error("Mount failed or timed out")
        cleanup()
        return 1

    logger.info("Archive mounted successfully")

    # Run rsync to restore data
    try:
        exit_code = run_rsync(mount_point, target_path)

        if exit_code != 0:
            logger.error("Restore failed during rsync")
            cleanup()
            return exit_code

        logger.info("Restore completed successfully!")

    except Exception as exc:
        logger.error(f"Restore failed: {exc}")
        cleanup()
        return 1

    # Cleanup and exit
    cleanup()
    return 0


def main() -> int:
    """Main entry point."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    # Parse args and load config
    args = parse_args()
    config = load_config(args.config)

    # Run restore
    return run_restore(config)


if __name__ == '__main__':
    sys.exit(main())
