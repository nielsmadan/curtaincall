from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from curtaincall.errors import ReleaseError, name_list, short_id
from curtaincall.git import (
    BRANCH_REFS,
    branch_ref,
    changed_paths,
    git_command,
    head_ref,
    is_ancestor,
    resolve_revision,
    tag_ref,
)


@dataclass(frozen=True, slots=True)
class Leftovers:
    branch: str
    start: str
    tip: str | None
    new_commits: int | None
    head: str | None
    head_ref: str | None
    tag: str | None
    uncommitted: frozenset[str]

    @property
    def on_branch(self) -> bool:
        return self.head_ref == branch_ref(self.branch)

    @property
    def changed(self) -> bool:
        return (
            self.tip != self.start
            or not self.on_branch
            or self.tag is not None
            or bool(self.uncommitted)
        )


def _branch_clause(leftovers: Leftovers) -> str:
    branch = f"Branch {leftovers.branch}"
    if leftovers.tip == leftovers.start:
        return f"{branch} is unchanged"
    if leftovers.tip is None:
        return f"{branch} was deleted"
    count = (
        ""
        if leftovers.new_commits is None
        else f" ({leftovers.new_commits} new commit{'' if leftovers.new_commits == 1 else 's'})"
    )
    moved = f"{short_id(leftovers.start)} to {short_id(leftovers.tip)}"
    return f"{branch} moved from {moved}{count}"


def _head_clauses(leftovers: Leftovers) -> list[str]:
    if leftovers.on_branch:
        return []
    position = (
        "detached"
        if leftovers.head_ref is None
        else (f"on branch {leftovers.head_ref.removeprefix(BRANCH_REFS)!r}")
    )
    commit = "" if leftovers.head is None else f" at {short_id(leftovers.head)}"
    return [f"HEAD is {position}{commit}"]


def describe_leftovers(leftovers: Leftovers | None) -> str:
    if leftovers is None:
        return (
            "Inspect the repository for preparation commits, a switched branch, "
            "uncommitted changes or a local tag."
        )
    clauses = [
        _branch_clause(leftovers),
        *_head_clauses(leftovers),
        *([f"local tag {leftovers.tag} was created"] if leftovers.tag is not None else []),
        f"uncommitted changes: {name_list(map(repr, sorted(leftovers.uncommitted)))}"
        if leftovers.uncommitted
        else "the worktree is clean",
    ]
    return "; ".join(clauses) + "."


def _new_commits(
    root: Path, start: str, tip: str | None, environment: Mapping[str, str]
) -> int | None:
    if tip is None or tip == start or not is_ancestor(root, start, tip, environment):
        return None
    return int(git_command(root, ("rev-list", "--count", f"{start}..{tip}"), environment).strip())


def collect_leftovers(
    root: Path, *, branch: str, start: str, tag: str, environment: Mapping[str, str]
) -> Leftovers | None:
    try:
        tip = resolve_revision(root, f"{branch_ref(branch)}^{{commit}}", environment)
        return Leftovers(
            branch=branch,
            start=start,
            tip=tip,
            new_commits=_new_commits(root, start, tip, environment),
            head=resolve_revision(root, "HEAD^{commit}", environment),
            head_ref=head_ref(root, environment),
            tag=tag if resolve_revision(root, tag_ref(tag), environment) is not None else None,
            uncommitted=changed_paths(root, environment),
        )
    except (ReleaseError, KeyboardInterrupt):
        return None
