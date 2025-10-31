#!/usr/bin/env python3
"""List borgbackup archives as JSON.

This script lists all archives in a borg repository and outputs structured JSON
for consumption by CLI tools or controllers.

Signal handling:
- SIGTERM/SIGINT: Terminates borg subprocess and cleans up lock
"""

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time

from common import load_config, setup_ssh_key, get_borg_env

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


def handle_shutdown(signum, frame):
    """Signal handler for graceful shutdown.

    When SIGTERM/SIGINT is received:
    1. Send SIGINT to borg process (graceful stop)
    2. Wait up to 20 seconds for borg to finish
    3. If still running, send SIGKILL and break lock

    Args:
        signum: Signal number
        frame: Current stack frame
    """
    global _borg_process, _borg_repo

    logger.info("Received termination signal, stopping borg gracefully...")

    if _borg_process and _borg_process.poll() is None:
        logger.info(f"Sending SIGINT to borg PID {_borg_process.pid}...")

        try:
            _borg_process.send_signal(signal.SIGINT)
        except Exception as exc:
            logger.warning(f"Failed to send SIGINT: {exc}")

        # Wait up to 20 seconds for graceful exit
        logger.info("Waiting up to 20 seconds for graceful exit...")
        for i in range(1, 21):
            if _borg_process.poll() is not None:
                logger.info(f"Borg stopped gracefully after {i}s")
                sys.exit(143)
            time.sleep(1)

        # Still running after 20s - force kill and cleanup
        if _borg_process.poll() is None:
            logger.info("Borg not stopped after 20s, forcing termination...")
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


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="List borgbackup archives as JSON")
    parser.add_argument(
        "-c", "--config",
        default="/config/config.yaml",
        help="Path to config file (default: /config/config.yaml)"
    )
    return parser.parse_args()


def list_archives(config: dict, env: dict) -> int:
    """List all archives in repository, output JSON to stdout.

    Args:
        config: Configuration dictionary
        env: Environment variables dict with BORG_* variables

    Returns:
        Exit code (0 = success)
    """
    global _borg_process, _borg_repo

    borg_repo = config['borgRepo']
    _borg_repo = borg_repo

    logger.info(f"Listing archives in repository: {borg_repo}")
    logger.info(f"PID: {os.getpid()}")

    try:
        # Run borg list with JSON output (no timeout)
        _borg_process = subprocess.Popen(
            ['borg', 'list', '--json', borg_repo],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )

        logger.info(f"Borg PID: {_borg_process.pid}")

        # Wait for borg to complete
        stdout, stderr = _borg_process.communicate()
        exit_code = _borg_process.returncode
        _borg_process = None

        if exit_code != 0:
            logger.error(f"borg list failed with exit code {exit_code}")
            logger.error(stderr)
            return exit_code

        # Parse JSON output from borg
        data = json.loads(stdout)
        archives = data.get('archives', [])

        logger.info(f"Found {len(archives)} archives")

        # Simplify output for CLI consumption
        output = {
            'repository': borg_repo,
            'archive_count': len(archives),
            'archives': [
                {
                    'name': archive['name'],
                    'time': archive['time'],
                    'id': archive['id'][:12],  # Short ID for readability
                }
                for archive in archives
            ]
        }

        # Output JSON to stdout (separate from logs which go to stderr via logging)
        print(json.dumps(output, indent=2))

        return 0

    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse borg JSON output: {exc}")
        return 1
    except Exception as exc:
        logger.error(f"Failed to list archives: {exc}")
        return 1


def main() -> int:
    """Main entry point."""
    # Register signal handlers
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    args = parse_args()
    config = load_config(args.config)

    # Setup SSH and environment
    ssh_key_file = setup_ssh_key(config['sshPrivateKey'])
    env = get_borg_env(config, ssh_key_file)

    # List archives directly (no init check needed for read-only operation)
    # If repo doesn't exist or is inaccessible, borg list will fail naturally
    return list_archives(config, env)


if __name__ == '__main__':
    sys.exit(main())
