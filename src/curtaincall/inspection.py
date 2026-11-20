import os
from collections.abc import Mapping
from pathlib import Path

from curtaincall.config import validate_paths
from curtaincall.errors import ReleaseError, name_list, short_id
from curtaincall.git import (
    BRANCH_REFS,
    OBJECT_ID,
    PEELED,
    TAG_REFS,
    branch_ref,
    changed_paths,
    git_command,
    git_process,
    head_ref,
    is_ancestor,
    is_shallow,
    require_repository,
    resolve_revision,
    tag_targets,
)
from curtaincall.models import (
    RefSnapshot,
    ReleaseConfig,
    ReleaseState,
    RepositorySnapshot,
    TagState,
    Version,
)
from curtaincall.versions import tag_version


def _is_rewritten(setting: str, origin: str) -> bool:
    key, _, value = setting.partition("\n")
    folded = key.casefold()
    return (
        folded.startswith("url.")
        and folded.endswith((".insteadof", ".pushinsteadof"))
        and origin.startswith(value)
    )


def _origin(root: Path, config: ReleaseConfig, environment: Mapping[str, str]) -> str:
    fetch = git_command(root, ("remote", "get-url", "--all", "origin"), environment).splitlines()
    push = git_command(
        root, ("remote", "get-url", "--push", "--all", "origin"), environment
    ).splitlines()
    if len(fetch) != 1 or len(push) != 1 or fetch != push:
        raise ReleaseError("Origin must have exactly one matching fetch and push destination.")
    origin = fetch[0]
    settings = git_command(root, ("config", "--null", "--list"), environment)
    if any(_is_rewritten(setting, origin) for setting in settings.split("\0")):
        raise ReleaseError(
            "Origin destination is subject to another URL rewrite; use a resolved destination."
        )
    if config.repository is not None:
        require_repository(origin, config.repository)
    return origin


def tag_states(selected: list[tuple[str, str, Version]], batch_output: str) -> tuple[TagState, ...]:
    targets = batch_output.splitlines()
    if len(targets) != len(selected):
        raise ReleaseError("Cannot resolve release tags: incomplete cat-file batch response.")
    tags = []
    for (name, object_id, version), target in zip(selected, targets, strict=True):
        commit, _, kind = target.partition(" ")
        if kind != "commit" or OBJECT_ID.fullmatch(commit) is None:
            raise ReleaseError(f"Release tag {name} does not resolve to an available commit.")
        tags.append(TagState(name, object_id, commit, version))
    return tuple(tags)


def _local_tags(
    root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> tuple[TagState, ...]:
    output = git_command(
        root,
        ("for-each-ref", "--format=%(refname) %(objectname)", f"{TAG_REFS}{config.tag_prefix}*"),
        environment,
    )
    refs = (line.split(" ", 1) for line in output.splitlines())
    selected = [
        (name, object_id, version)
        for ref, object_id in refs
        if (version := tag_version(config, name := ref.removeprefix(TAG_REFS))) is not None
    ]
    if not selected:
        return ()
    batch = git_command(
        root,
        ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
        environment,
        input_data=os.fsencode("".join(f"{oid}^{{commit}}\n" for _, oid, _ in selected)),
    )
    return tag_states(selected, batch)


def _is_release_ref(config: ReleaseConfig, ref: str) -> bool:
    return ref == branch_ref(config.branch) or (
        ref.startswith(TAG_REFS)
        and tag_version(config, ref.removeprefix(TAG_REFS).removesuffix(PEELED)) is not None
    )


def _remote_refs(
    root: Path, origin: str, config: ReleaseConfig, environment: Mapping[str, str]
) -> RefSnapshot:
    output = git_command(
        root,
        ("ls-remote", "--", origin, branch_ref(config.branch), f"{TAG_REFS}{config.tag_prefix}*"),
        environment,
    )
    lines = (line.split("\t", 1) for line in output.splitlines())
    return tuple(
        sorted((ref, object_id) for object_id, ref in lines if _is_release_ref(config, ref))
    )


def _tag_list(config: ReleaseConfig, names: set[str]) -> str:
    return name_list(sorted(names, key=lambda name: tag_version(config, name) or ()))


def _validate_history(
    root: Path,
    config: ReleaseConfig,
    tags: tuple[TagState, ...],
    refs: RefSnapshot,
    environment: Mapping[str, str],
) -> str:
    branch = dict(refs).get(branch_ref(config.branch))
    if branch is None:
        raise ReleaseError(f"Origin has no {config.branch} branch.")
    present = git_process(root, ("cat-file", "-e", f"{branch}^{{commit}}"), environment)
    if present.code:
        raise ReleaseError(
            f"Origin's {config.branch} is at {short_id(branch)}, which is not in this repository; "
            f"fetch origin and integrate origin/{config.branch} before releasing."
        )
    if not is_ancestor(root, branch, "HEAD", environment):
        raise ReleaseError(
            f"HEAD does not contain origin's {config.branch} ({short_id(branch)}); "
            f"integrate origin/{config.branch} before releasing."
        )
    remote = tag_targets(refs)
    local = {tag.name: tag for tag in tags}
    if remote.keys() != local.keys():
        raise ReleaseError(
            "Local and origin release tags differ "
            f"(only on origin: {_tag_list(config, set(remote) - set(local))}; "
            f"only local: {_tag_list(config, set(local) - set(remote))}); "
            "inspect and synchronize tags."
        )
    for name, tag in local.items():
        if remote[name][1] != tag.commit_id:
            raise ReleaseError(f"Release tag {name} has different local and origin targets.")
    return branch


def _inspect(
    root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> tuple[RepositorySnapshot, str]:
    validate_paths(root, config)
    ref = head_ref(root, environment)
    if ref is None:
        raise ReleaseError(f"Release requires branch {config.branch}; HEAD is detached.")
    branch = ref.removeprefix(BRANCH_REFS)
    if branch != config.branch:
        raise ReleaseError(f"Release requires branch {config.branch}; current branch is {branch}.")
    head = resolve_revision(root, "HEAD^{commit}", environment)
    if head is None:
        raise ReleaseError(f"Release requires a commit on {config.branch}; the branch has none.")
    if is_shallow(root, environment):
        raise ReleaseError("Release requires a complete, non-shallow repository.")
    if changed_paths(root, environment):
        raise ReleaseError(
            "Release requires a clean worktree and index, including untracked files."
        )
    origin = _origin(root, config, environment)
    tags = _local_tags(root, config, environment)
    refs = _remote_refs(root, origin, config, environment)
    remote_branch = _validate_history(root, config, tags, refs, environment)
    return RepositorySnapshot(head, origin, refs, tags), remote_branch


def inspect_snapshot(
    root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> RepositorySnapshot:
    return _inspect(root, config, environment)[0]


def inspect_repository(
    root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> ReleaseState:
    snapshot, remote_branch = _inspect(root, config, environment)
    head = snapshot.head
    latest_tag = max(snapshot.local_tags, key=lambda tag: tag.version, default=None)
    revision_range = "HEAD"
    if latest_tag is not None:
        if not is_ancestor(root, latest_tag.commit_id, head, environment):
            raise ReleaseError(
                f"HEAD does not contain the latest release tag {latest_tag.name} "
                f"({short_id(latest_tag.commit_id)}); release from a commit that includes it."
            )
        revision_range = f"{latest_tag.commit_id}..{head}"
    output = git_command(
        root,
        (
            "log",
            "--no-show-signature",
            "-z",
            "--format=%B",
            revision_range,
            "--",
            *config.history_paths,
        ),
        environment,
    )
    messages = tuple(message.strip() for message in output.split("\0")[:-1])
    if not any(messages):
        raise ReleaseError("There are no relevant commits to release.")
    ahead = int(
        git_command(root, ("rev-list", "--count", f"{remote_branch}..{head}"), environment).strip()
    )
    return ReleaseState(
        snapshot, None if latest_tag is None else latest_tag.version, messages, ahead
    )
