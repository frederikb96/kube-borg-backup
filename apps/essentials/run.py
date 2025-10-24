#!/usr/bin/env python3
"""Borgbackup essentials container - Python rewrite of run.sh.

This script orchestrates BorgBackup operations:
1. Reads configuration from mounted config file
2. Sets up SSH authentication
3. Checks/initializes Borg repository
4. Creates backup archive
5. Applies retention policy
6. Handles graceful shutdown on SIGTERM/SIGINT
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)

# Global state for signal handling
_borg_process: Optional[subprocess.Popen] = None
_borg_repo: Optional[str] = None


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Borgbackup essentials container")
    parser.add_argument(
        "-c", "--config",
        default="/config/config.yaml",
        help="Path to config file (default: /config/config.yaml)"
    )
    return parser.parse_args()


def load_config(config_path: str) -> Dict:
    """Load and validate configuration from YAML file.

    Args:
        config_path: Path to configuration file

    Returns:
        Configuration dictionary

    Raises:
        SystemExit: If config file not found or invalid
    """
    path = Path(config_path)

    if not path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    try:
        with path.open('r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
    except Exception as exc:
        logger.error(f"Failed to parse config file: {exc}")
        sys.exit(1)

    # Validate required fields
    required = ['borgRepo', 'borgPassphrase', 'sshPrivateKey', 'prefix', 'backupDir', 'lockWait']
    missing = [field for field in required if field not in config]
    if missing:
        logger.error(f"Config missing required fields: {', '.join(missing)}")
        sys.exit(1)

    return config


def setup_ssh_key(ssh_key_content: str) -> str:
    """Write SSH private key to file and set permissions.

    Args:
        ssh_key_content: SSH private key as string

    Returns:
        Path to SSH key file

    Raises:
        SystemExit: If SSH key setup fails
    """
    ssh_dir = Path("/root/.ssh")
    ssh_key_file = ssh_dir / "borg-ssh.key"

    try:
        # Create .ssh directory
        ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

        # Write SSH key
        ssh_key_file.write_text(ssh_key_content, encoding='utf-8')
        ssh_key_file.chmod(0o600)

        logger.info(f"SSH key written to {ssh_key_file}")
        logger.info(f"SSH key file size: {ssh_key_file.stat().st_size} bytes")

        return str(ssh_key_file)

    except Exception as exc:
        logger.error(f"Failed to setup SSH key: {exc}")
        sys.exit(1)


def check_repo_status(borg_repo: str, borg_passphrase: str, borg_rsh: str) -> bool:
    """Check repository status and initialize if needed.

    Uses 'borg info' to check if repository is ready. Handles three scenarios:
    1. Exit 0 -> Repository ready
    2. Exit 2 + "is not a valid repository" -> Initialize repository
    3. Exit 2 + "Failed to create/acquire the lock" -> Repository locked (proceed anyway)
    4. Any other error -> Fail

    Args:
        borg_repo: Borg repository URL
        borg_passphrase: Repository passphrase
        borg_rsh: SSH command for Borg

    Returns:
        True if repository is ready

    Raises:
        SystemExit: If repository check fails unexpectedly
    """
    env = os.environ.copy()
    env['BORG_REPO'] = borg_repo
    env['BORG_PASSPHRASE'] = borg_passphrase
    env['BORG_RSH'] = borg_rsh

    logger.info("Checking repository status with 'borg info'...")

    try:
        result = subprocess.run(
            ['borg', 'info', borg_repo],
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )

        # Success - repository ready
        if result.returncode == 0:
            logger.info("Repository ready")
            return True

        # Exit 2 - check error message
        if result.returncode == 2:
            output = result.stderr + result.stdout

            # Repository not initialized
            if "is not a valid repository" in output:
                logger.info("Repository not initialized, initializing...")
                try:
                    init_result = subprocess.run(
                        ['borg', 'init', '--encryption', 'repokey-blake2', borg_repo],
                        env=env,
                        capture_output=True,
                        text=True,
                        timeout=60
                    )

                    if init_result.returncode != 0:
                        logger.error(f"Failed to initialize repository: {init_result.stderr}")
                        sys.exit(1)

                    logger.info("Repository initialized successfully")
                    return True

                except Exception as exc:
                    logger.error(f"Failed to initialize repository: {exc}")
                    sys.exit(1)

            # Repository locked - proceed anyway (borg create will wait)
            elif "Failed to create/acquire the lock" in output:
                logger.info("Repository locked, will wait during backup")
                return True

            # Other exit 2 error - fail
            else:
                logger.error(f"Unexpected borg info failure (exit 2):")
                logger.error(output)
                sys.exit(1)

        # Any other exit code - fail
        logger.error(f"borg info failed with exit code {result.returncode}:")
        logger.error(result.stderr + result.stdout)
        sys.exit(1)

    except subprocess.TimeoutExpired:
        logger.error("borg info timed out after 60 seconds")
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Failed to check repository status: {exc}")
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


def run_backup(config: Dict) -> int:
    """Run borg backup with configuration.

    Args:
        config: Configuration dictionary

    Returns:
        Exit code (0 = success)
    """
    global _borg_process, _borg_repo

    # Extract config
    borg_repo = config['borgRepo']
    borg_passphrase = config['borgPassphrase']
    prefix = config['prefix']
    backup_dir = config['backupDir']
    lock_wait = config['lockWait']
    retention = config.get('retention', {})

    _borg_repo = borg_repo

    # Setup SSH key
    ssh_key_file = setup_ssh_key(config['sshPrivateKey'])

    # Build BORG_RSH with required SSH flags
    borg_rsh = f"ssh -o IdentityFile={ssh_key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"

    # Setup environment
    env = os.environ.copy()
    env['BORG_REPO'] = borg_repo
    env['BORG_PASSPHRASE'] = borg_passphrase
    env['BORG_RSH'] = borg_rsh
    env['BORG_CACHE_DIR'] = '/cache'  # Cache mounted at /cache by Kubernetes

    logger.info(f"Starting backup: {prefix}")
    logger.info(f"Lock wait timeout: {lock_wait}s")
    logger.info(f"PID: {os.getpid()}")

    # Check repository status
    check_repo_status(borg_repo, borg_passphrase, borg_rsh)

    # Build archive name with UTC timestamp
    archive_name = f"{prefix}-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H-%M-%S')}"
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

        # Wait for borg to complete
        exit_code = _borg_process.wait()
        _borg_process = None

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
