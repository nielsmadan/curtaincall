import os
import shlex
import shutil
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from urllib.parse import quote, urlencode

from curtaincall.errors import NothingToRelease, ReleaseCancelled, ReleaseError, short_id
from curtaincall.git import (
    BRANCH_REFS,
    PEELED,
    TAG_REFS,
    branch_ref,
    git_command,
    head_commit,
    object_type,
    tag_ref,
)
from curtaincall.inspection import inspect_repository, inspect_snapshot
from curtaincall.leftovers import collect_leftovers, describe_leftovers
from curtaincall.models import (
    PinUpdate,
    Proposal,
    RefSnapshot,
    ReleaseConfig,
    ReleaseRequest,
    ReleaseState,
    RepositorySnapshot,
    Version,
)
from curtaincall.pins import pending_updates, pin_message, pin_stage
from curtaincall.process import run_command
from curtaincall.stages import prepare
from curtaincall.versions import (
    classify,
    format_version,
    make_proposal,
    release_tag,
    select_version,
)

type Prompt = Callable[[str], str]


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
    if ref.startswith(TAG_REFS):
        return f"origin's tag {ref.removeprefix(TAG_REFS)}"
    return f"origin's {ref.removeprefix(BRANCH_REFS)}"


def _unpeeled(refs: RefSnapshot) -> dict[str, str]:
    return {ref: object_id for ref, object_id in refs if not ref.endswith(PEELED)}


def _state_changes(
    reviewed: RepositorySnapshot, current: RepositorySnapshot, head: str
) -> list[str]:
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


def _require_reviewed(reviewed: RepositorySnapshot, current: RepositorySnapshot, head: str) -> None:
    changes = _state_changes(reviewed, current, head)
    if changes:
        raise ReleaseError(
            f"The reviewed repository state changed ({'; '.join(changes)}); "
            "inspect it and start a new release."
        )


@dataclass(frozen=True, slots=True)
class _Prepared:
    commit: str
    tag_object: str


def _require_tools(root: Path, config: ReleaseConfig, environment: Mapping[str, str]) -> None:
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
        raise ReleaseError(f"The policy requires signing, but Git cannot sign: {error}") from error


_KEEP_PINS: Final = "To release without updating pins, run curtains with --keep-pins."


@dataclass(frozen=True, slots=True)
class _PinPlan:
    updates: tuple[PinUpdate, ...]
    summary: str | None

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(pin_message(update) for update in self.updates)


def _pin_plan(
    root: Path, config: ReleaseConfig, request: ReleaseRequest, environment: Mapping[str, str]
) -> _PinPlan:
    if not config.pins:
        return _PinPlan((), None)
    if request.keep_pins:
        return _PinPlan((), "Pins: not checked (--keep-pins)")
    try:
        updates = pending_updates(root, config.pins, environment)
    except ReleaseError as error:
        raise ReleaseError(f"{error}\n{_KEEP_PINS}") from error
    summary = "; ".join(
        f"{update.pin.name} {format_version(update.pin.version)} -> {format_version(update.latest)}"
        for update in updates
    )
    return _PinPlan(updates, f"Pins: {summary or 'current'}")


def _preview(
    config: ReleaseConfig, state: ReleaseState, proposal: Proposal, pins: _PinPlan
) -> None:
    version = (
        release_tag(config, proposal.version)
        if proposal.version is not None
        else "no automatic bump"
    )
    previous = release_tag(config, state.latest) if state.latest is not None else "first release"
    print(f"\n{config.name}: {previous} -> {version}")
    scope = " (history paths)" if config.history_paths else ""
    since = "previous release" if state.latest else "repository start"
    print(f"Commits since {since}{scope}: {len(state.messages)}")
    print(f"Branch: {config.branch}; {state.ahead} commit(s) ahead of origin")
    if config.sign:
        print("Signing: required for preparation commits and the release tag")
    if pins.summary is not None:
        print(pins.summary)
    from_pins = f" ({len(pins.updates)} from pin updates)" if pins.updates else ""
    print(
        f"Changes: {proposal.counts.features} feature(s), "
        f"{proposal.counts.fixes} fix(es){from_pins}, "
        f"{proposal.counts.breaking} breaking change(s)"
    )
    if proposal.suggestion_skipped and config.suggest is not None:
        print(f"Built-in estimate only; dry-run skipped suggestion: {shlex.join(config.suggest)}")
    changes = [(message.partition("\n")[0], classify(message)) for message in state.messages]
    for label, subjects in (
        ("Features", [subject for subject, counts in changes if counts.features]),
        ("Breaking changes", [subject for subject, counts in changes if counts.breaking]),
    ):
        print(f"{label}:" if subjects else f"{label}: none")
        for subject in subjects:
            print(f"  - {subject}")


def _confirm(
    config: ReleaseConfig,
    state: ReleaseState,
    proposal: Proposal,
    prompt: Prompt,
    pins: _PinPlan,
) -> Version | None:
    candidate = proposal.version
    while True:
        answer = prompt(
            "Release? [y/N/version] " if candidate else "Release version (Enter cancels): "
        ).strip()
        if not answer or answer.lower() in {"n", "no"}:
            print("Release cancelled.")
            return None
        if answer.lower() in {"y", "yes"} and candidate is not None:
            return candidate
        try:
            candidate = select_version(config, answer, state.latest)
        except ReleaseError as error:
            print(error)
            continue
        _preview(
            config,
            state,
            make_proposal(
                config, state, override=format_version(candidate), pending_messages=pins.messages
            ),
            pins,
        )


def _create_tag(
    root: Path, *, sign: bool, head: str, tag: str, environment: Mapping[str, str]
) -> str:
    git_command(
        root,
        ("tag", "--sign" if sign else "--annotate", "-m", f"Release {tag}", "--", tag, head),
        environment,
    )
    tag_object = git_command(root, ("rev-parse", "--verify", tag_ref(tag)), environment).strip()
    if (
        object_type(root, tag_object, environment) != "tag"
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


def _update_pin(
    root: Path,
    config: ReleaseConfig,
    update: PinUpdate,
    version: Version,
    environment: Mapping[str, str],
) -> None:
    stage = pin_stage(update)
    try:
        prepare(root, replace(config, stages=(stage,)), version, sys.executable, environment)
    except ReleaseError as error:
        raise ReleaseError(
            f"Updating {update.pin.name} to {format_version(update.latest)} failed: {error}\n"
            "To release without updating pins, discard the preparation changes and run "
            "curtains with --keep-pins."
        ) from error


def _prepare_and_tag(
    root: Path,
    config: ReleaseConfig,
    reviewed: RepositorySnapshot,
    version: Version,
    updates: tuple[PinUpdate, ...],
    environment: Mapping[str, str],
) -> _Prepared:
    tag = release_tag(config, version)
    try:
        for update in updates:
            _update_pin(root, config, update, version, environment)
        prepare(root, config, version, sys.executable, environment)
        prepared = head_commit(root, environment)
        _require_reviewed(reviewed, inspect_snapshot(root, config, environment), prepared)
        return _Prepared(
            prepared,
            _create_tag(root, sign=config.sign, head=prepared, tag=tag, environment=environment),
        )
    except (KeyboardInterrupt, ReleaseError) as error:
        leftovers = collect_leftovers(
            root, branch=config.branch, start=reviewed.head, tag=tag, environment=environment
        )
        description = describe_leftovers(leftovers)
        if isinstance(error, KeyboardInterrupt):
            raise ReleaseCancelled(
                f"Release cancelled before the push. {description}",
                left_changes=leftovers is None or leftovers.changed,
            ) from error
        raise ReleaseError(f"{error}\nCurtains did not push the release. {description}") from error


def _report(config: ReleaseConfig, tag: str, head: str) -> None:
    print(f"Pushed {tag} at {head}.")
    print(config.success_message)
    if config.repository is not None:
        base = f"https://github.com/{quote(config.repository, safe='/')}"
        print(f"Tag: {base}/releases/tag/{quote(tag, safe='')}")
        if config.workflow is not None:
            query = urlencode({"query": "branch:" + tag})
            workflow = quote(config.workflow, safe="")
            print(f"Publication runs asynchronously: {base}/actions/workflows/{workflow}?{query}")
        if config.draft:
            print(f"Review and publish the draft when the workflow finishes: {base}/releases")


def _suggestion(root: Path, config: ReleaseConfig, environment: Mapping[str, str]) -> str | None:
    return (
        None if config.suggest is None else run_command(root, config.suggest, environment).strip()
    )


def release(
    root: Path,
    config: ReleaseConfig,
    request: ReleaseRequest,
    environment: Mapping[str, str],
    *,
    prompt: Prompt | None,
) -> None:
    if not request.dry_run and not request.yes and prompt is None:
        raise ReleaseError(
            "Interactive confirmation requires a terminal; use --yes for unattended releases."
        )
    reviewed = inspect_repository(root, config, environment)
    pins = _pin_plan(root, config, request, environment)
    if not any(reviewed.messages) and not pins.updates:
        raise ReleaseError("There are no relevant commits to release.")
    use_suggestion = (
        config.suggest is not None and reviewed.latest is not None and request.version is None
    )
    if not request.dry_run:
        _require_tools(root, config, environment)
        if config.sign:
            _require_signing(root, environment)
    proposal = make_proposal(
        config,
        reviewed,
        override=request.version,
        suggested=_suggestion(root, config, environment)
        if use_suggestion and not request.dry_run
        else None,
        suggestion_skipped=request.dry_run and use_suggestion,
        pending_messages=pins.messages,
    )
    _preview(config, reviewed, proposal, pins)
    if request.dry_run:
        print("Dry run: checks, preparation, tagging and publication skipped.")
        return
    if request.yes and proposal.version is None:
        raise NothingToRelease(
            "No automatic release; supply a version override to release these changes."
        )
    for command in config.preflight:
        print(f"Checking: {shlex.join(command)}", flush=True)
        run_command(root, command, environment, capture=False)
    if config.preflight:
        _preview(config, reviewed, proposal, pins)
    version = (
        proposal.version
        if request.yes or prompt is None
        else _confirm(config, reviewed, proposal, prompt, pins)
    )
    if version is None:
        return
    snapshot = reviewed.snapshot
    _require_reviewed(snapshot, inspect_snapshot(root, config, environment), snapshot.head)
    prepared = _prepare_and_tag(root, config, snapshot, version, pins.updates, environment)
    tag = release_tag(config, version)
    _push(
        root,
        snapshot.origin,
        {branch_ref(config.branch): prepared.commit, tag_ref(tag): prepared.tag_object},
        environment,
    )
    _report(config, tag, prepared.commit)
