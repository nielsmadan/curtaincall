import argparse
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from curtaincall.config import load_config
from curtaincall.errors import ReleaseError
from curtaincall.git import (
    command_environment,
    is_ancestor,
    is_shallow,
    object_type,
    require_origin_repository,
    resolve_location,
    resolve_revision,
    tag_names,
)
from curtaincall.models import ReleaseConfig
from curtaincall.publish import OBJECT_ID, Publication, default_title, publish, read_assets
from curtaincall.versions import previous_tag, require_release_tag


def add_location_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", type=Path, help="Release JSON, relative to the starting directory"
    )
    parser.add_argument("--directory", type=Path, default=Path.cwd(), help="Starting directory")


def _require_full_history(root: Path, environment: Mapping[str, str]) -> None:
    if is_shallow(root, environment):
        raise ReleaseError(
            "The repository is shallow; use a full-history checkout (fetch-depth: 0)."
        )


def _require_event_shape(tag: str, event: str | None) -> None:
    ref = f"refs/tags/{tag}"
    if event is not None and OBJECT_ID.fullmatch(event) is None and event != ref:
        raise ReleaseError(f"Invalid release event {event!r}; expected a full commit SHA or {ref}.")


def _annotated_tag_commit(root: Path, tag: str, environment: Mapping[str, str]) -> str:
    ref = f"refs/tags/{tag}"
    if resolve_revision(root, ref, environment) is None:
        raise ReleaseError(f"Release tag {tag} does not exist locally; fetch tags.")
    kind = object_type(root, ref, environment)
    if kind != "tag":
        raise ReleaseError(f"Release ref {tag} must be an annotated tag, got {kind!r}.")
    commit = resolve_revision(root, f"{ref}^{{commit}}", environment)
    if commit is None:
        raise ReleaseError(f"Release tag {tag} does not identify a commit.")
    return commit


def _require_event(
    root: Path, tag: str, event: str | None, commit: str, environment: Mapping[str, str]
) -> None:
    if event is not None and resolve_revision(root, f"{event}^{{commit}}", environment) != commit:
        raise ReleaseError(f"Release event {event} does not identify {tag} commit {commit}.")


def _require_contained(
    root: Path, config: ReleaseConfig, commit: str, environment: Mapping[str, str]
) -> None:
    branch_ref = f"refs/remotes/origin/{config.branch}"
    branch_commit = resolve_revision(root, f"{branch_ref}^{{commit}}", environment)
    if branch_commit is None:
        raise ReleaseError(
            f"{branch_ref} does not exist; fetch the {config.branch} branch from origin."
        )
    if not is_ancestor(root, commit, branch_commit, environment):
        raise ReleaseError(
            f"Release commit {commit} is not contained in origin/{config.branch} ({branch_commit})."
        )


def release_commit(
    root: Path, config: ReleaseConfig, tag: str, event: str | None, environment: Mapping[str, str]
) -> str:
    require_release_tag(config, tag)
    _require_event_shape(tag, event)
    _require_full_history(root, environment)
    commit = _annotated_tag_commit(root, tag, environment)
    _require_event(root, tag, event, commit, environment)
    if config.repository is not None:
        require_origin_repository(root, config.repository, environment)
    _require_contained(root, config, commit, environment)
    return commit


def _find_previous_tag(
    root: Path, config: ReleaseConfig, tag: str, environment: Mapping[str, str]
) -> str | None:
    require_release_tag(config, tag)
    _require_full_history(root, environment)
    return previous_tag(config, tag, tag_names(root, environment))


def _run_validate_tag(
    args: argparse.Namespace, root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> str:
    return release_commit(root, config, args.tag, args.event, environment) + "\n"


def _run_previous_tag(
    args: argparse.Namespace, root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> str:
    found = _find_previous_tag(root, config, args.tag, environment)
    return "" if found is None else found + "\n"


def _run_publish(
    args: argparse.Namespace, root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> str:
    notes = (args.directory / args.notes).absolute()
    publication = Publication(
        tag=args.tag,
        target=args.target,
        title=default_title(config, args.tag) if args.title is None else args.title,
        notes=notes,
        body=notes.read_bytes().decode("utf-8"),
        assets=read_assets([(args.directory / path).absolute() for path in args.assets]),
        prerelease=args.prerelease,
        latest=not args.not_latest,
    )
    publish(root, config, publication, environment)
    return ""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curtains ci", description="Verify and publish releases in CI."
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    validate = commands.add_parser(
        "validate-tag",
        help="Print the release commit of a valid, contained release tag",
        description="Print the release commit of a valid, contained release tag.",
    )
    add_location_arguments(validate)
    validate.add_argument("tag", metavar="TAG")
    validate.add_argument(
        "event", nargs="?", metavar="EVENT", help="Full commit SHA or refs/tags/TAG"
    )
    validate.set_defaults(handler=_run_validate_tag)
    previous = commands.add_parser(
        "previous-tag",
        help="Print the highest release tag lower than TAG",
        description="Print the highest release tag lower than TAG, or nothing.",
    )
    add_location_arguments(previous)
    previous.add_argument("tag", metavar="TAG")
    previous.set_defaults(handler=_run_previous_tag)
    publication = commands.add_parser(
        "publish",
        help="Create, complete and publish the GitHub release for TAG",
        description=(
            "Create, complete and publish the GitHub release for TAG with gh. "
            "Relative FILE and ASSET paths resolve against --directory."
        ),
    )
    add_location_arguments(publication)
    publication.add_argument("tag", metavar="TAG")
    publication.add_argument("target", metavar="TARGET", help="Full commit SHA the tag identifies")
    publication.add_argument(
        "--notes", required=True, type=Path, metavar="FILE", help="Release notes file"
    )
    publication.add_argument("--title", help="Release title (default: NAME VERSION)")
    publication.add_argument("--prerelease", action="store_true", help="Mark as a prerelease")
    publication.add_argument(
        "--not-latest",
        action="store_true",
        help="Do not mark as the latest release (gh --latest=false)",
    )
    publication.add_argument(
        "assets", nargs="*", type=Path, metavar="ASSET", help="Files to attach"
    )
    publication.set_defaults(handler=_run_publish)
    return parser


def _execute(args: argparse.Namespace, environment: Mapping[str, str]) -> str:
    location = resolve_location(args.directory, args.config, environment)
    config = load_config(location.config_path)
    output: str = args.handler(args, location.root, config, environment)
    return output


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        sys.stdout.write(_execute(args, command_environment(os.environ)))
    except KeyboardInterrupt:
        print("curtains: interrupted", file=sys.stderr)
        return 130
    except (ReleaseError, OSError, UnicodeError, ValueError) as error:
        print(f"curtains: {error}", file=sys.stderr)
        return 1
    return 0
