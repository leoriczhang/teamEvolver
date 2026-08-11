"""
teamEvolver CLI entry point.

Usage:
    teamEvolver config KEY VAL — set a config value
    teamEvolver config show    — show current config
    teamEvolver skills ...     — manage shared skills
"""

from __future__ import annotations

import sys

try:
    import click
except ImportError:
    print("teamEvolver requires 'click'. Install it with: pip install click")
    sys.exit(1)

from .config_cmd import config_cmd
from .daemon import start, status, stop
from .diag import doctor, restore, validation
from .skills_cmd import skills


@click.group()
def main():
    """teamEvolver shared library tooling."""


main.add_command(config_cmd)
main.add_command(start)
main.add_command(stop)
main.add_command(status)
main.add_command(doctor)
main.add_command(restore)
main.add_command(validation)
main.add_command(skills)


if __name__ == "__main__":
    main()
