import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from curtaincall.config import template_values, validate_paths
from curtaincall.errors import ReleaseError, name_list
from curtaincall.git import (
    branch_ref,
    changed_paths,
    diff_paths,
    git_command,
    head_commit,
    head_ref,
)
from curtaincall.models import Command, ReleaseConfig, Stage, Version
from curtaincall.process import run_command

type Substitutions = Mapping[str, str]


def _require_position(root: Path, branch: str, head: str, environment: Mapping[str, str]) -> None:
    if head_ref(root, environment) != branch_ref(branch) or head_commit(root, environment) != head:
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
            f"Preparation changed undeclared paths: {name_list(map(repr, unexpected))}"
        )


def _run_stage_commands(
    root: Path,
    commands: tuple[Command, ...],
    substitutions: Substitutions,
    environment: Mapping[str, str],
) -> None:
    for command in commands:
        argv = tuple(value.format_map(substitutions) for value in command)
        print(f"Running: {shlex.join(argv)}", flush=True)
        run_command(root, argv, environment, capture=False)


def _commit_integrity(
    root: Path, parent: str, allowed: tuple[str, ...], environment: Mapping[str, str]
) -> str:
    head = head_commit(root, environment)
    parents = git_command(root, ("rev-list", "--parents", "-1", head), environment).split()
    if parents != [head, parent]:
        raise ReleaseError(
            "Preparation commit must have exactly the captured HEAD as its single parent."
        )
    _require_allowed(diff_paths(root, parent, head, environment), allowed)
    if changed_paths(root, environment):
        raise ReleaseError("Preparation left a dirty worktree or index; inspect local artifacts.")
    return head


@dataclass(frozen=True, slots=True)
class _StageRun:
    root: Path
    config: ReleaseConfig
    stage: Stage
    substitutions: Substitutions
    revision: str
    environment: Mapping[str, str]

    def require_position(self, head: str) -> None:
        _require_position(self.root, self.config.branch, head, self.environment)


def _run_checked(run: _StageRun, command: Command) -> None:
    validate_paths(run.root, run.config)
    _run_stage_commands(run.root, (command,), run.substitutions, run.environment)
    run.require_position(run.revision)
    validate_paths(run.root, run.config)
    _require_allowed(changed_paths(run.root, run.environment), run.stage.files)


def _verify_commit(run: _StageRun) -> str:
    head = _commit_integrity(run.root, run.revision, run.stage.files, run.environment)
    run.require_position(head)
    validate_paths(run.root, run.config)
    return head


def _commit_stage(run: _StageRun) -> None:
    root, environment = run.root, run.environment
    paths = changed_paths(root, environment)
    if not paths:
        return
    git_command(root, ("add", "--all", "--", *sorted(paths)), environment)
    tree = git_command(root, ("write-tree",), environment).strip()
    git_command(
        root,
        (
            "commit",
            *(("--gpg-sign",) if run.config.sign else ()),
            "-m",
            run.stage.message.format_map(run.substitutions),
        ),
        environment,
        capture=False,
    )
    head = _verify_commit(run)
    if git_command(root, ("rev-parse", f"{head}^{{tree}}"), environment).strip() != tree:
        _run_stage_commands(root, run.stage.checks, run.substitutions, environment)
        run.require_position(head)
        _verify_commit(run)


def prepare(
    root: Path, config: ReleaseConfig, version: Version, python: str, environment: Mapping[str, str]
) -> None:
    for stage in config.stages:
        revision = head_commit(root, environment)
        run = _StageRun(
            root,
            config,
            stage,
            template_values(config, version=version, revision=revision, python=python),
            revision,
            environment,
        )
        for command in (*stage.commands, *stage.checks):
            _run_checked(run, command)
        _commit_stage(run)
