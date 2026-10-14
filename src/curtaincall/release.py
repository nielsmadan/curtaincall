import os
import shlex
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote, urlencode

from curtaincall.config import validate_paths, validate_template
from curtaincall.errors import ReleaseError
from curtaincall.git import changed_paths, git_command, inspect_repository, run_command
from curtaincall.models import (
    Command,
    Location,
    Proposal,
    ReleaseConfig,
    ReleaseRequest,
    ReleaseState,
)
from curtaincall.versions import make_proposal, select_version


def _head(root: Path, environment: Mapping[str, str]) -> str:
    return git_command(root, ("rev-parse", "--verify", "HEAD^{commit}"), environment).strip()


def _require_position(root: Path, branch: str, head: str, environment: Mapping[str, str]) -> None:
    current_branch = git_command(root, ("symbolic-ref", "--quiet", "HEAD"), environment).strip()
    if current_branch != f"refs/heads/{branch}" or _head(root, environment) != head:
        raise ReleaseError(
            "Preparation changed HEAD or switched branches; inspect local artifacts."
        )


def _require_allowed(paths: frozenset[str], allowed: tuple[str, ...]) -> None:
    unexpected = sorted(
        path
        for path in paths
        if not any(path == item or path.startswith(item + "/") for item in allowed)
    )
    if unexpected:
        raise ReleaseError(
            f"Preparation changed undeclared paths: {', '.join(map(repr, unexpected))}"
        )


def _format(value: str, version: str, revision: str, python: str) -> str:
    validate_template(value, "stage template")
    return value.format(version=version, revision=revision, python=python)


def _stage_commands(
    root: Path,
    commands: tuple[Command, ...],
    substitutions: tuple[str, str, str],
    environment: Mapping[str, str],
) -> None:
    for command in commands:
        argv = tuple(_format(value, *substitutions) for value in command)
        print(f"Running: {shlex.join(argv)}", flush=True)
        run_command(root, argv, environment, capture=False)


def _commit_integrity(
    root: Path, parent: str, allowed: tuple[str, ...], environment: Mapping[str, str]
) -> str:
    head = _head(root, environment)
    parents = git_command(root, ("rev-list", "--parents", "-1", head), environment).split()
    if parents != [head, parent]:
        raise ReleaseError(
            "Preparation commit must have exactly the captured HEAD as its single parent."
        )
    output = git_command(
        root,
        (
            "diff",
            "--name-only",
            "-z",
            "--no-renames",
            "--ignore-submodules=none",
            parent,
            head,
            "--",
        ),
        environment,
    )
    _require_allowed(frozenset(path for path in output.split("\0") if path), allowed)
    if changed_paths(root, environment):
        raise ReleaseError("Preparation left a dirty worktree or index; inspect local artifacts.")
    return head


def prepare(
    root: Path, config: ReleaseConfig, version: str, python: str, environment: Mapping[str, str]
) -> None:
    for stage in config.stages:
        revision = _head(root, environment)
        substitutions = (version, revision, python)
        for commands in (stage.commands, stage.checks):
            for command in commands:
                validate_paths(root, config)
                _stage_commands(root, (command,), substitutions, environment)
                _require_position(root, config.branch, revision, environment)
                validate_paths(root, config)
                _require_allowed(changed_paths(root, environment), stage.files)
        paths = changed_paths(root, environment)
        if not paths:
            continue
        git_command(root, ("add", "--all", "--", *sorted(paths)), environment)
        tree = git_command(root, ("write-tree",), environment).strip()
        git_command(
            root,
            ("commit", "-m", _format(stage.message, *substitutions)),
            environment,
            capture=False,
        )
        head = _commit_integrity(root, revision, stage.files, environment)
        _require_position(root, config.branch, head, environment)
        validate_paths(root, config)
        if git_command(root, ("rev-parse", f"{head}^{{tree}}"), environment).strip() != tree:
            _stage_commands(root, stage.checks, substitutions, environment)
            _require_position(root, config.branch, head, environment)
            _commit_integrity(root, revision, stage.files, environment)
            validate_paths(root, config)


def _require_reviewed(reviewed: ReleaseState, current: ReleaseState, head: str) -> None:
    if (
        current.head != head
        or current.branch != reviewed.branch
        or current.origin != reviewed.origin
        or current.remote_refs != reviewed.remote_refs
        or current.local_tags != reviewed.local_tags
    ):
        raise ReleaseError(
            "The reviewed repository state changed; inspect it and start a new release."
        )


def _tools(root: Path, config: ReleaseConfig, environment: Mapping[str, str]) -> None:
    search_path = os.pathsep.join(
        str(root / part) for part in environment.get("PATH", os.defpath).split(os.pathsep)
    )
    for tool in config.tools:
        executable = str(root / tool) if "/" in tool else tool
        if shutil.which(executable, path=search_path) is None:
            raise ReleaseError(f"Required release tool is unavailable: {tool}")


def _preview(config: ReleaseConfig, state: ReleaseState, proposal: Proposal) -> None:
    version = (
        config.tag_prefix + proposal.version
        if proposal.version is not None
        else "no automatic bump"
    )
    print(f"{config.name}: {state.latest or 'first release'} -> {version}")
    print(f"Branch: {state.branch}; {state.ahead} commit(s) ahead of origin")
    print(
        f"Changes: {proposal.counts.features} feature(s), {proposal.counts.fixes} fix(es), "
        f"{proposal.counts.breaking} breaking change(s)"
    )
    if proposal.suggestion_skipped and config.suggest is not None:
        print(f"Built-in estimate only; dry-run skipped suggestion: {shlex.join(config.suggest)}")


def _confirm(config: ReleaseConfig, state: ReleaseState, proposal: Proposal) -> str | None:
    if not sys.stdin.isatty():
        raise ReleaseError(
            "Interactive confirmation requires a terminal; use --yes for unattended releases."
        )
    candidate = proposal.version
    while True:
        answer = input(
            "Release? [y/N/version] " if candidate else "Release version (Enter cancels): "
        ).strip()
        if not answer or answer.lower() in {"n", "no"}:
            print("Release cancelled.")
            return None
        if answer.lower() in {"y", "yes"} and candidate is not None:
            return candidate
        candidate = select_version(answer, state.latest, config)
        _preview(config, state, make_proposal(config, state, candidate, None, False))


def _publish(
    root: Path,
    config: ReleaseConfig,
    reviewed: ReleaseState,
    head: str,
    version: str,
    environment: Mapping[str, str],
) -> None:
    tag = config.tag_prefix + version
    git_command(
        root,
        ("tag", "--annotate", "--no-sign", "-m", f"Release {tag}", "--", tag, head),
        environment,
    )
    tag_object = git_command(
        root, ("rev-parse", "--verify", f"refs/tags/{tag}"), environment
    ).strip()
    if (
        git_command(root, ("cat-file", "-t", tag_object), environment).strip() != "tag"
        or git_command(root, ("rev-parse", f"{tag_object}^{{commit}}"), environment).strip() != head
    ):
        raise ReleaseError(
            "The new annotated tag does not target the prepared commit; inspect local artifacts."
        )
    try:
        git_command(
            root,
            (
                "push",
                "--atomic",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "--",
                reviewed.origin,
                f"{head}:refs/heads/{config.branch}",
                f"{tag_object}:refs/tags/{tag}",
            ),
            environment,
            capture=False,
        )
    except (ReleaseError, KeyboardInterrupt) as error:
        raise ReleaseError(
            f"{error}\nPublication failed; local commits and tag are preserved. "
            "The remote outcome may be uncertain: inspect remote refs before taking further action."
        ) from error
    _report(config, tag, head)


def _report(config: ReleaseConfig, tag: str, head: str) -> None:
    print(f"Pushed {tag} at {head}.")
    print(config.publication)
    if config.repository is not None:
        base = f"https://github.com/{quote(config.repository, safe='/')}"
        print(f"Tag: {base}/releases/tag/{quote(tag, safe='')}")
        if config.workflow is not None:
            print(
                f"Publication runs asynchronously: {base}/actions/workflows/{quote(config.workflow, safe='')}?{urlencode({'query': 'branch:' + tag})}"
            )
        if config.draft:
            print(f"Review and publish the draft when the workflow finishes: {base}/releases")


def release(
    location: Location,
    config: ReleaseConfig,
    request: ReleaseRequest,
    environment: Mapping[str, str],
) -> None:
    root = location.root
    reviewed = inspect_repository(root, config, environment)
    suggested = None
    use_suggestion = (
        config.suggest is not None and reviewed.latest is not None and request.version is None
    )
    if not request.dry_run:
        _tools(root, config, environment)
        if use_suggestion and config.suggest is not None:
            suggested = run_command(root, config.suggest, environment).strip()
    proposal = make_proposal(
        config, reviewed, request.version, suggested, request.dry_run and use_suggestion
    )
    _preview(config, reviewed, proposal)
    if request.dry_run:
        print("Dry run: checks, preparation, tagging and publication skipped.")
        return
    if request.yes and proposal.version is None:
        print("No automatic release; supply a version override to release these changes.")
        return
    for command in config.checks:
        print(f"Checking: {shlex.join(command)}", flush=True)
        run_command(root, command, environment, capture=False)
    version = proposal.version if request.yes else _confirm(config, reviewed, proposal)
    if version is None:
        return
    _require_reviewed(reviewed, inspect_repository(root, config, environment), reviewed.head)
    prepare(root, config, version, sys.executable, environment)
    prepared = _head(root, environment)
    _require_reviewed(reviewed, inspect_repository(root, config, environment), prepared)
    _publish(root, config, reviewed, prepared, version, environment)
