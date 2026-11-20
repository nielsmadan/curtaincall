import argparse
from pathlib import Path


def add_location_arguments(parser: argparse.ArgumentParser, *, defaults: bool = True) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=None if defaults else argparse.SUPPRESS,
        help="Release policy JSON, relative to the starting directory",
    )
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path.cwd() if defaults else argparse.SUPPRESS,
        help="Starting directory",
    )
