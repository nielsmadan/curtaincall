import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from curtaincall.config import validate_paths, validate_template
from curtaincall.errors import ReleaseCancelled, ReleaseError, name_list, short_id
from curtaincall.git import (
    changed_paths,
    git_command,
    head_ref,
    inspect_repository,
    is_ancestor,
    resolve_revision,
    run_command,
)
from curtaincall.models import (
    Command,
    Location,
    Proposal,
    RefSnapshot,
    ReleaseConfig,
    ReleaseRequest,
    ReleaseState,
)
from curtaincall.versions import count_changes, make_proposal, select_version


def _head(root: Path, environment: Mapping[str, str]) -> str:
    return git_command(root, ("rev-parse", "--verify", "HEAD^{commit}"), environment).strip()


def _require_position(root: Path, branch: str, head: str, environment: Mapping[str, str]) -> None:
    if head_ref(root, environment) != f"refs/heads/{branch}" or _head(root, environment) != head:
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


type Substitutions = Mapping[str, str]


def _format(value: str, substitutions: Substitutions) -> str:
    validate_template(value, "stage template")
    return value.format_map(substitutions)


def _stage_commands(
    root: Path,
    commands: tuple[Command, ...],
    substitutions: Substitutions,
    environment: Mapping[str, str],
) -> None:
    for command in commands:
        argv = tuple(_format(value, substitutions) for value in command)
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
        substitutions = {
            "version": version,
            "revision": revision,
            "python": python,
            "tag": config.tag_prefix + version,
            "tag_prefix": config.tag_prefix,
        }
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
            (
                "commit",
                *(("--gpg-sign",) if config.sign else ()),
                "-m",
                _format(stage.message, substitutions),
            ),
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


def _ref_changes(
    before: Mapping[str, str], after: Mapping[str, str], label: Callable[[str], str]
) -> list[str]:
    return [
        *(
            f"{label(name)} appeared at {short_id(after[name])}"
            for name in sorted(after.keys() - before.keys())
        ),
        *(
            f"{label(name)} was deleted (was {short_id(before[name])})"
            for name in sorted(before.keys() - after.keys())
        ),
        *(
            f"{label(name)} moved from {short_id(before[name])} to {short_id(after[name])}"
            for name in sorted(before.keys() & after.keys())
            if before[name] != after[name]
        ),
    ]


def _remote_label(ref: str) -> str:
    if ref.startswith("refs/tags/"):
        return f"origin's tag {ref.removeprefix('refs/tags/')}"
    return f"origin's {ref.removeprefix('refs/heads/')}"


def _unpeeled(refs: RefSnapshot) -> dict[str, str]:
    return {ref: object_id for ref, object_id in refs if not ref.endswith("^{}")}


def _state_changes(reviewed: ReleaseState, current: ReleaseState, head: str) -> list[str]:
    return [
        *(
            [f"HEAD moved from {short_id(head)} to {short_id(current.head)}"]
            if current.head != head
            else []
        ),
        *(["origin's URL changed"] if current.origin != reviewed.origin else []),
        *_ref_changes(
            _unpeeled(reviewed.remote_refs), _unpeeled(current.remote_refs), _remote_label
        ),
        *_ref_changes(
            {tag.name: tag.object_id for tag in reviewed.local_tags},
            {tag.name: tag.object_id for tag in current.local_tags},
            lambda name: f"local tag {name}",
        ),
    ]


def _require_reviewed(reviewed: ReleaseState, current: ReleaseState, head: str) -> None:
    changes = _state_changes(reviewed, current, head)
    if changes:
        raise ReleaseError(
            f"The reviewed repository state changed ({'; '.join(changes)}); "
            "inspect it and start a new release."
        )


@dataclass(frozen=True, slots=True)
class _Leftovers:
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
        return self.head_ref == f"refs/heads/{self.branch}"

    @property
    def changed(self) -> bool:
        return (
            self.tip != self.start
            or not self.on_branch
            or self.tag is not None
            or bool(self.uncommitted)
        )


def _branch_clause(leftovers: _Leftovers) -> str:
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


def _head_clauses(leftovers: _Leftovers) -> list[str]:
    if leftovers.on_branch:
        return []
    position = (
        "detached"
        if leftovers.head_ref is None
        else (f"on branch {leftovers.head_ref.removeprefix('refs/heads/')!r}")
    )
    commit = "" if leftovers.head is None else f" at {short_id(leftovers.head)}"
    return [f"HEAD is {position}{commit}"]


def _describe_leftovers(leftovers: _Leftovers | None) -> str:
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


def _leftovers(
    root: Path, *, branch: str, start: str, tag: str, environment: Mapping[str, str]
) -> _Leftovers | None:
    try:
        tip = resolve_revision(root, f"refs/heads/{branch}^{{commit}}", environment)
        return _Leftovers(
            branch=branch,
            start=start,
            tip=tip,
            new_commits=_new_commits(root, start, tip, environment),
            head=resolve_revision(root, "HEAD^{commit}", environment),
            head_ref=head_ref(root, environment),
            tag=tag
            if resolve_revision(root, f"refs/tags/{tag}", environment) is not None
            else None,
            uncommitted=changed_paths(root, environment),
        )
    except (ReleaseError, KeyboardInterrupt):
        return None


@dataclass(frozen=True, slots=True)
class _Prepared:
    commit: str
    tag_object: str


def _tools(root: Path, config: ReleaseConfig, environment: Mapping[str, str]) -> None:
    search_path = os.pathsep.join(
        str(root / part) for part in environment.get("PATH", os.defpath).split(os.pathsep)
    )
    for tool in config.tools:
        executable = str(root / tool) if "/" in tool else tool
        if shutil.which(executable, path=search_path) is None:
            raise ReleaseError(f"Required release tool is unavailable: {tool}")


def _require_signing(root: Path, environment: Mapping[str, str]) -> None:
    try:
        git_command(
            root, ("commit-tree", "--gpg-sign", "-m", "Signing check", "HEAD^{tree}"), environment
        )
    except ReleaseError as error:
        raise ReleaseError(f"config.sign requires signing, but Git cannot sign: {error}") from error


def _preview(config: ReleaseConfig, state: ReleaseState, proposal: Proposal) -> None:
    version = (
        config.tag_prefix + proposal.version
        if proposal.version is not None
        else "no automatic bump"
    )
    previous = config.tag_prefix + state.latest if state.latest is not None else "first release"
    print(f"\n{config.name}: {previous} -> {version}")
    scope = " (configured paths)" if config.paths else ""
    print(
        f"Commits since {'previous release' if state.latest else 'repository start'}{scope}: {len(state.messages)}"
    )
    print(f"Branch: {state.branch}; {state.ahead} commit(s) ahead of origin")
    if config.sign:
        print("Signing: required for preparation commits and the release tag")
    print(
        f"Changes: {proposal.counts.features} feature(s), {proposal.counts.fixes} fix(es), "
        f"{proposal.counts.breaking} breaking change(s)"
    )
    if proposal.suggestion_skipped and config.suggest is not None:
        print(f"Built-in estimate only; dry-run skipped suggestion: {shlex.join(config.suggest)}")
    changes = [
        (message.partition("\n")[0], count_changes((message,))) for message in state.messages
    ]
    for label, subjects in (
        ("Features", [subject for subject, counts in changes if counts.features]),
        ("Breaking changes", [subject for subject, counts in changes if counts.breaking]),
    ):
        print(f"{label}:" if subjects else f"{label}: none")
        for subject in subjects:
            print(f"  - {subject}")


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


def _create_tag(root: Path, sign: bool, head: str, tag: str, environment: Mapping[str, str]) -> str:
    git_command(
        root,
        ("tag", "--sign" if sign else "--annotate", "-m", f"Release {tag}", "--", tag, head),
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
    return tag_object


def _push(
    root: Path, origin: str, targets: Mapping[str, str], environment: Mapping[str, str]
) -> None:
    try:
        git_command(
            root,
            (
                "push",
                "--atomic",
                "--no-follow-tags",
                "--recurse-submodules=no",
                "--",
                origin,
                *(f"{object_id}:{ref}" for ref, object_id in targets.items()),
            ),
            environment,
            capture=False,
        )
    except (ReleaseError, KeyboardInterrupt) as error:
        raise ReleaseError(
            f"{error}\nPublication failed; local commits and tag are preserved. "
            "The remote outcome may be uncertain: inspect remote refs before taking further action."
        ) from error


def _prepare_and_tag(
    root: Path,
    config: ReleaseConfig,
    reviewed: ReleaseState,
    version: str,
    tag: str,
    environment: Mapping[str, str],
) -> _Prepared:
    try:
        prepare(root, config, version, sys.executable, environment)
        prepared = _head(root, environment)
        _require_reviewed(reviewed, inspect_repository(root, config, environment), prepared)
        return _Prepared(prepared, _create_tag(root, config.sign, prepared, tag, environment))
    except KeyboardInterrupt as error:
        leftovers = _leftovers(
            root, branch=config.branch, start=reviewed.head, tag=tag, environment=environment
        )
        raise ReleaseCancelled(
            f"Release cancelled before the push. {_describe_leftovers(leftovers)}",
            left_changes=leftovers is None or leftovers.changed,
        ) from error
    except ReleaseError as error:
        leftovers = _leftovers(
            root, branch=config.branch, start=reviewed.head, tag=tag, environment=environment
        )
        raise ReleaseError(
            f"{error}\nCurtains did not push the release. {_describe_leftovers(leftovers)}"
        ) from error


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
        if config.sign:
            _require_signing(root, environment)
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
    if config.checks:
        _preview(config, reviewed, proposal)
    version = proposal.version if request.yes else _confirm(config, reviewed, proposal)
    if version is None:
        return
    _require_reviewed(reviewed, inspect_repository(root, config, environment), reviewed.head)
    tag = config.tag_prefix + version
    prepared = _prepare_and_tag(root, config, reviewed, version, tag, environment)
    _push(
        root,
        reviewed.origin,
        {f"refs/heads/{config.branch}": prepared.commit, f"refs/tags/{tag}": prepared.tag_object},
        environment,
    )
    _report(config, tag, prepared.commit)
