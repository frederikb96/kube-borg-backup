#!/usr/bin/env python3
"""List borgbackup archives as JSON.

This script lists all archives in a borg repository and outputs structured JSON
for consumption by CLI tools or controllers.
"""

import argparse
import json
import logging
import subprocess
import sys

from common import load_config, setup_ssh_key, get_borg_env, init_borg_repo

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s] %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)


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
    borg_repo = config['borgRepo']

    logger.info(f"Listing archives in repository: {borg_repo}")

    try:
        # Run borg list with JSON output
        result = subprocess.run(
            ['borg', 'list', '--json', borg_repo],
            env=env,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            logger.error(f"borg list failed with exit code {result.returncode}")
            logger.error(result.stderr)
            return result.returncode

        # Parse JSON output from borg
        data = json.loads(result.stdout)
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

    except subprocess.TimeoutExpired:
        logger.error("borg list timed out after 30 seconds")
        return 1
    except json.JSONDecodeError as exc:
        logger.error(f"Failed to parse borg JSON output: {exc}")
        return 1
    except Exception as exc:
        logger.error(f"Failed to list archives: {exc}")
        return 1


def main() -> int:
    """Main entry point."""
    args = parse_args()
    config = load_config(args.config)

    # Setup SSH and environment
    ssh_key_file = setup_ssh_key(config['sshPrivateKey'])
    env = get_borg_env(config, ssh_key_file)

    # Check/initialize repository
    init_borg_repo(config, env)

    # List archives
    return list_archives(config, env)


if __name__ == '__main__':
    sys.exit(main())
