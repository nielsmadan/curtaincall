import argparse
import os
import sys
from collections.abc import Sequence
from importlib.metadata import version
from typing import Final

from curtaincall import ci
from curtaincall.arguments import add_location_arguments
from curtaincall.config import load_config
from curtaincall.errors import NothingToRelease, ReleaseCancelled, ReleaseError
from curtaincall.git import resolve_location
from curtaincall.models import ReleaseRequest
from curtaincall.process import command_environment
from curtaincall.release import release

NOTHING_RELEASED: Final = 3


_LOCATION_OPTIONS: Final = ("--config", "--directory")


def _ci_arguments(arguments: list[str]) -> list[str] | None:
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token in _LOCATION_OPTIONS:
            index += 2
        elif token.startswith(tuple(f"{option}=" for option in _LOCATION_OPTIONS)):
            index += 1
        else:
            break
    if arguments[index : index + 1] != ["ci"]:
        return None
    return [*arguments[:index], *arguments[index + 1 :]]


def main(argv: Sequence[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else list(argv)
    ci_arguments = _ci_arguments(arguments)
    if ci_arguments is not None:
        return ci.main(ci_arguments)
    parser = argparse.ArgumentParser(
        prog="curtains",
        usage=(
            "%(prog)s [options] [patch|minor|major|VERSION]\n"
            "       %(prog)s [--config CONFIG] [--directory DIRECTORY] ci COMMAND ..."
        ),
        description="Prepare, tag and push a Git release.",
        epilog="CI commands: run 'curtains ci --help'.",
    )
    parser.add_argument("version", nargs="?", metavar="patch|minor|major|VERSION")
    add_location_arguments(parser)
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview using read-only Git inspection"
    )
    parser.add_argument("--yes", action="store_true", help="Confirm an unattended release")
    parser.add_argument(
        "--version",
        action="version",
        version=f"curtains {version('curtaincall')}",
        help="Show the curtains version and exit",
    )
    args = parser.parse_args(arguments)
    try:
        environment = command_environment(os.environ)
        location = resolve_location(args.directory, args.config, environment)
        release(
            location.root,
            load_config(location.config_path),
            ReleaseRequest(version=args.version, dry_run=args.dry_run, yes=args.yes),
            environment,
            prompt=input if sys.stdin.isatty() else None,
        )
    except NothingToRelease as nothing:
        print(nothing)
        return NOTHING_RELEASED
    except ReleaseCancelled as cancelled:
        print(f"\n{cancelled}", file=sys.stderr)
        return 130 if cancelled.left_changes else 0
    except (EOFError, KeyboardInterrupt):
        print("\nRelease cancelled.", file=sys.stderr)
        return 0
    except ReleaseError as error:
        print(f"curtains: {error}", file=sys.stderr)
        return 1
    return 0
