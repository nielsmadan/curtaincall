import argparse
import os
import sys
from collections.abc import Sequence
from importlib.metadata import version

from curtaincall import ci
from curtaincall.config import load_config
from curtaincall.errors import ReleaseError
from curtaincall.git import command_environment, resolve_location
from curtaincall.models import ReleaseRequest
from curtaincall.release import release


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    if arguments[:1] == ["ci"]:
        return ci.main(arguments[1:])
    parser = argparse.ArgumentParser(
        prog="curtains",
        description="Prepare, tag and push a Git release.",
        epilog="CI commands: run 'curtains ci --help'.",
    )
    parser.add_argument("version", nargs="?", metavar="patch|minor|major|VERSION")
    ci.add_location_arguments(parser)
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview using read-only Git inspection"
    )
    parser.add_argument("--yes", action="store_true", help="Confirm an unattended release")
    parser.add_argument("--version", action="version", version=f"curtains {version('curtaincall')}")
    args = parser.parse_args(arguments)
    try:
        environment = command_environment(os.environ)
        location = resolve_location(args.directory, args.config, environment)
        config = load_config(location.config_path)
        release(location, config, ReleaseRequest(args.version, args.dry_run, args.yes), environment)
    except (EOFError, KeyboardInterrupt):
        print("\nRelease cancelled.", file=sys.stderr)
        return 0
    except ReleaseError as error:
        print(f"curtains: {error}", file=sys.stderr)
        return 1
    return 0
