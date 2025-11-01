#!/usr/bin/env python3
"""Shared utilities for borgbackup operations.

This module provides common functionality used by all borg operation scripts:
- Configuration file loading and validation
- SSH key setup
- Environment variable construction
- Repository initialization
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

import yaml

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


def load_config(config_path: str = '/config/config.yaml') -> dict:
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

    # Validate required base fields (needed for all operations)
    required = ['borgRepo', 'borgPassphrase', 'sshPrivateKey']
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


def get_borg_env(config: dict, ssh_key_file: str, cache_dir: str = '/cache') -> dict:
    """Build environment variables for borg commands.

    Args:
        config: Configuration dictionary with borgRepo and borgPassphrase
        ssh_key_file: Path to SSH private key file
        cache_dir: Path to borg cache directory (default: /cache)

    Returns:
        Environment dictionary with BORG_* variables set
    """
    env = os.environ.copy()
    env['BORG_REPO'] = config['borgRepo']
    env['BORG_PASSPHRASE'] = config['borgPassphrase']
    env['BORG_RSH'] = f"ssh -o IdentityFile={ssh_key_file} -o IdentitiesOnly=yes -o StrictHostKeyChecking=no"
    env['BORG_CACHE_DIR'] = cache_dir

    return env


def init_borg_repo(config: dict, env: dict) -> None:
    """Check repository status and initialize if needed.

    Uses 'borg info' to check if repository is ready. Handles three scenarios:
    1. Exit 0 -> Repository ready
    2. Exit 2 + "is not a valid repository" -> Initialize repository
    3. Exit 2 + "Failed to create/acquire the lock" -> Repository locked (proceed anyway)
    4. Any other error -> Fail

    Args:
        config: Configuration dictionary
        env: Environment variables dict with BORG_* variables

    Raises:
        SystemExit: If repository check fails unexpectedly
    """
    borg_repo = config['borgRepo']

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
            return

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
                    return

                except Exception as exc:
                    logger.error(f"Failed to initialize repository: {exc}")
                    sys.exit(1)

            # Repository locked - proceed anyway (borg commands will wait)
            elif "Failed to create/acquire the lock" in output:
                logger.info("Repository locked, will wait during operation")
                return

            # Other exit 2 error - fail
            else:
                logger.error("Unexpected borg info failure (exit 2):")
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
