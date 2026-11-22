import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Final

from curtaincall.errors import ReleaseError
from curtaincall.models import Command, Location
from curtaincall.process import ProcessResult, process_failure, run_command, run_process

GIT_PREFIX: Final[Command] = (
    "git",
    "--no-replace-objects",
    "--literal-pathspecs",
    "--no-optional-locks",
    "--no-lazy-fetch",
)
OBJECT_ID: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
BRANCH_REFS: Final = "refs/heads/"
TAG_REFS: Final = "refs/tags/"
PEELED: Final = "^{}"


def branch_ref(name: str) -> str:
    return BRANCH_REFS + name


def tag_ref(name: str) -> str:
    return TAG_REFS + name


def tag_targets(refs: Iterable[tuple[str, str]]) -> dict[str, tuple[str, str]]:
    objects = dict(refs)
    return {
        ref.removeprefix(TAG_REFS): (object_id, objects.get(ref + PEELED, object_id))
        for ref, object_id in objects.items()
        if ref.startswith(TAG_REFS) and not ref.endswith(PEELED)
    }


def _display(argv: Command) -> str:
    return shlex.join(("git", *argv))


def git_command(
    root: Path,
    argv: Command,
    environment: Mapping[str, str],
    *,
    capture: bool = True,
    input_data: bytes | None = None,
) -> str:
    return run_command(
        root,
        (*GIT_PREFIX, *argv),
        environment,
        capture=capture,
        input_data=input_data,
        display=_display(argv),
    )


def git_process(root: Path, argv: Command, environment: Mapping[str, str]) -> ProcessResult:
    return run_process(root, (*GIT_PREFIX, *argv), environment, display=_display(argv))


def is_shallow(root: Path, environment: Mapping[str, str]) -> bool:
    return (
        git_command(root, ("rev-parse", "--is-shallow-repository"), environment).strip() == "true"
    )


def resolve_revision(root: Path, revision: str, environment: Mapping[str, str]) -> str | None:
    result = git_process(root, ("rev-parse", "--verify", "--quiet", revision), environment)
    return result.stdout.strip() if result.code == 0 else None


def head_commit(root: Path, environment: Mapping[str, str]) -> str:
    return git_command(root, ("rev-parse", "--verify", "HEAD^{commit}"), environment).strip()


def object_type(root: Path, revision: str, environment: Mapping[str, str]) -> str:
    return git_command(root, ("cat-file", "-t", revision), environment).strip()


def head_ref(root: Path, environment: Mapping[str, str]) -> str | None:
    result = git_process(root, ("symbolic-ref", "--quiet", "HEAD"), environment)
    if result.code == 1:
        return None
    if result.code:
        raise ReleaseError(process_failure("git symbolic-ref HEAD", result))
    return result.stdout.strip()


def is_ancestor(root: Path, ancestor: str, descendant: str, environment: Mapping[str, str]) -> bool:
    result = git_process(root, ("merge-base", "--is-ancestor", ancestor, descendant), environment)
    if result.code not in {0, 1}:
        raise ReleaseError(process_failure("git merge-base --is-ancestor", result))
    return result.code == 0


def tag_names(root: Path, environment: Mapping[str, str]) -> list[str]:
    return git_command(
        root, ("for-each-ref", "--format=%(refname:strip=2)", TAG_REFS), environment
    ).splitlines()


def remote_tag_lines(root: Path, tag: str, environment: Mapping[str, str]) -> list[str]:
    ref = tag_ref(tag)
    result = git_process(root, ("ls-remote", "--tags", "origin", ref, ref + PEELED), environment)
    if result.code:
        raise ReleaseError(process_failure("git ls-remote origin", result))
    return result.stdout.splitlines()


def resolve_location(
    directory: Path,
    config_path: Path | None,
    environment: Mapping[str, str],
    *,
    default: str = "scripts/release.json",
    label: str = "release policy",
) -> Location:
    try:
        start = directory.resolve()
        root = Path(
            git_command(start, ("rev-parse", "--show-toplevel"), environment).removesuffix("\n")
        ).resolve()
        selected = (start / config_path if config_path is not None else root / default).resolve()
    except (OSError, RuntimeError) as error:
        raise ReleaseError(f"Cannot resolve release location: {error}") from error
    if not selected.is_relative_to(root) or any(
        part.casefold() == ".git" for part in selected.relative_to(root).parts
    ):
        raise ReleaseError(f"The {label} must be inside the selected worktree.")
    return Location(root, selected)


def _status_path(entry: str) -> str:
    return entry[3:]


def changed_paths(root: Path, environment: Mapping[str, str]) -> frozenset[str]:
    output = git_command(
        root,
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
            "--no-renames",
        ),
        environment,
    )
    return frozenset(_status_path(entry) for entry in output.split("\0") if entry)


def diff_paths(root: Path, base: str, head: str, environment: Mapping[str, str]) -> frozenset[str]:
    output = git_command(
        root,
        ("diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=none", base, head, "--"),
        environment,
    )
    return frozenset(path for path in output.split("\0") if path)


def matches_repository(origin: str, repository: str) -> bool:
    pattern = (
        r"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/)"
        + re.escape(repository)
        + r"(?:\.git)?"
    )
    return re.fullmatch(pattern, origin) is not None


def require_repository(origin: str, repository: str) -> None:
    if not matches_repository(origin, repository):
        raise ReleaseError(f"Origin must be the GitHub repository {repository}.")


def require_origin_repository(root: Path, repository: str, environment: Mapping[str, str]) -> None:
    require_repository(
        git_command(root, ("remote", "get-url", "origin"), environment).strip(), repository
    )
