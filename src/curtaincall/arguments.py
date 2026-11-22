import argparse
from pathlib import Path


def add_location_arguments(
    parser: argparse.ArgumentParser,
    *,
    defaults: bool = True,
    file_option: str = "--config",
    file_help: str = "Release policy JSON, relative to the starting directory",
) -> None:
    parser.add_argument(
        file_option,
        type=Path,
        default=None if defaults else argparse.SUPPRESS,
        help=file_help,
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path.cwd() if defaults else argparse.SUPPRESS,
        help="Starting directory",
    )
