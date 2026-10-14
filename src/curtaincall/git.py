import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from pathlib import Path

from curtaincall.config import validate_paths, validate_ref
from curtaincall.errors import ReleaseError
from curtaincall.models import Command, Location, RefSnapshot, ReleaseConfig, ReleaseState, TagState
from curtaincall.versions import parse_version


def command_environment(source: Mapping[str, str]) -> dict[str, str]:
    preserved = {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_ALLOW_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_TERMINAL_PROMPT",
        "GIT_PROXY_COMMAND",
        "GIT_CURL_FTP_NO_EPSV",
        "GIT_GSS_DELEGATION",
    }
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("GIT_")
        or key in preserved
        or key.startswith(("GIT_SSL_", "GIT_PROXY_SSL_", "GIT_HTTP_"))
    }


def _redact_urls(value: str) -> str:
    return re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@", r"\1[redacted]@", value)


def run_command(
    root: Path,
    argv: Command,
    environment: Mapping[str, str],
    *,
    capture: bool = True,
    input_data: bytes | None = None,
) -> str:
    try:
        result = subprocess.run(
            argv, cwd=root, env=environment, capture_output=capture, check=False, input=input_data
        )
    except (OSError, ValueError) as error:
        raise ReleaseError(_redact_urls(f"Cannot run {shlex.join(argv)}: {error}")) from error
    if result.returncode:
        detail = os.fsdecode(result.stderr or result.stdout or b"").strip()
        raise ReleaseError(
            _redact_urls(
                f"Command failed ({result.returncode}): {shlex.join(argv)}"
                + (f"\n{detail}" if detail else "")
            )
        )
    return os.fsdecode(result.stdout) if capture else ""


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
        (
            "git",
            "--no-replace-objects",
            "--literal-pathspecs",
            "--no-optional-locks",
            "--no-lazy-fetch",
            *argv,
        ),
        environment,
        capture=capture,
        input_data=input_data,
    )


def resolve_location(
    directory: Path, config_path: Path | None, environment: Mapping[str, str]
) -> Location:
    try:
        start = directory.resolve()
        root = Path(
            git_command(start, ("rev-parse", "--show-toplevel"), environment).removesuffix("\n")
        ).resolve()
        selected = (
            start / config_path if config_path is not None else root / "scripts/release.json"
        ).resolve()
    except (OSError, RuntimeError) as error:
        raise ReleaseError(f"Cannot resolve release location: {error}") from error
    if not selected.is_relative_to(root) or any(
        part.casefold() == ".git" for part in selected.relative_to(root).parts
    ):
        raise ReleaseError("The release config must be inside the selected worktree.")
    return Location(root, selected)


def changed_paths(root: Path, environment: Mapping[str, str]) -> frozenset[str]:
    outputs = (
        git_command(
            root,
            ("diff", "--name-only", "-z", "--no-renames", "--ignore-submodules=none", "--"),
            environment,
        ),
        git_command(
            root,
            (
                "diff",
                "--cached",
                "--name-only",
                "-z",
                "--no-renames",
                "--ignore-submodules=none",
                "--",
            ),
            environment,
        ),
        git_command(root, ("ls-files", "--others", "--exclude-standard", "-z", "--"), environment),
    )
    return frozenset(path for output in outputs for path in output.split("\0") if path)


def _origin(root: Path, config: ReleaseConfig, environment: Mapping[str, str]) -> str:
    fetch = git_command(root, ("remote", "get-url", "--all", "origin"), environment).splitlines()
    push = git_command(
        root, ("remote", "get-url", "--push", "--all", "origin"), environment
    ).splitlines()
    if len(fetch) != 1 or len(push) != 1 or fetch != push:
        raise ReleaseError("Origin must have exactly one matching fetch and push destination.")
    origin = fetch[0]
    settings = git_command(root, ("config", "--null", "--list"), environment)
    for setting in settings.split("\0"):
        key, _, value = setting.partition("\n")
        if (
            key.casefold().startswith("url.")
            and key.casefold().endswith((".insteadof", ".pushinsteadof"))
            and origin.startswith(value)
        ):
            raise ReleaseError(
                "Origin destination is subject to another URL rewrite; use a resolved destination."
            )
    if config.repository is not None:
        repository = re.escape(config.repository)
        pattern = rf"(?:git@github\.com:|ssh://git@github\.com/|https://github\.com/){repository}(?:\.git)?"
        if re.fullmatch(pattern, origin) is None:
            raise ReleaseError(f"Origin must be the GitHub repository {config.repository}.")
    return origin


def _tag_version(name: str, config: ReleaseConfig) -> tuple[int, ...] | None:
    if not name.startswith(config.tag_prefix):
        return None
    try:
        return parse_version(name[len(config.tag_prefix) :], config.components)
    except ReleaseError:
        return None


def _local_tags(
    root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> tuple[TagState, ...]:
    output = git_command(
        root, ("for-each-ref", "--format=%(refname) %(objectname)", "refs/tags/"), environment
    )
    selected = []
    for line in output.splitlines():
        ref, object_id = line.split(" ", 1)
        name = ref.removeprefix("refs/tags/")
        if _tag_version(name, config) is not None:
            selected.append((name, object_id))
    if not selected:
        return ()
    output = git_command(
        root,
        ("cat-file", "--batch-check=%(objectname) %(objecttype)"),
        environment,
        input_data=os.fsencode("".join(f"{oid}^{{commit}}\n" for _, oid in selected)),
    )
    targets = output.splitlines()
    if len(targets) != len(selected):
        raise ReleaseError("Cannot resolve release tags: incomplete cat-file batch response.")
    tags = []
    for (name, object_id), target in zip(selected, targets, strict=True):
        commit, _, kind = target.partition(" ")
        if kind != "commit" or re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit) is None:
            raise ReleaseError(f"Release tag {name} does not resolve to an available commit.")
        tags.append(TagState(name, object_id, commit))
    return tuple(tags)


def _remote_refs(
    root: Path, origin: str, config: ReleaseConfig, environment: Mapping[str, str]
) -> RefSnapshot:
    output = git_command(root, ("ls-remote", "--", origin), environment)
    refs = []
    for line in output.splitlines():
        object_id, ref = line.split("\t", 1)
        name = ref.removeprefix("refs/tags/").removesuffix("^{}")
        if ref == f"refs/heads/{config.branch}" or (
            ref.startswith("refs/tags/") and _tag_version(name, config) is not None
        ):
            refs.append((ref, object_id))
    return tuple(sorted(refs))


def _validate_history(
    root: Path,
    config: ReleaseConfig,
    tags: tuple[TagState, ...],
    refs: RefSnapshot,
    environment: Mapping[str, str],
) -> str:
    remote = dict(refs)
    branch = remote.get(f"refs/heads/{config.branch}")
    if branch is None:
        raise ReleaseError(f"Origin has no {config.branch} branch.")
    git_command(root, ("cat-file", "-e", f"{branch}^{{commit}}"), environment)
    git_command(root, ("merge-base", "--is-ancestor", branch, "HEAD"), environment)
    remote_tags = {
        ref.removeprefix("refs/tags/")
        for ref in remote
        if ref.startswith("refs/tags/") and not ref.endswith("^{}")
    }
    if remote_tags != {tag.name for tag in tags}:
        raise ReleaseError(
            "Local and origin release tag sets differ; inspect and synchronize tags."
        )
    for tag in tags:
        ref = f"refs/tags/{tag.name}"
        target = remote.get(f"{ref}^{{}}", remote[ref])
        if target != tag.commit_id:
            raise ReleaseError(f"Release tag {tag.name} has different local and origin targets.")
    return branch


def inspect_repository(
    root: Path, config: ReleaseConfig, environment: Mapping[str, str]
) -> ReleaseState:
    validate_ref(config.branch, "config.branch", branch=True)
    validate_ref(config.tag_prefix + config.initial_version, "config.tag_prefix")
    validate_paths(root, config)
    head = git_command(root, ("rev-parse", "--verify", "HEAD^{commit}"), environment).strip()
    branch = (
        git_command(root, ("symbolic-ref", "--quiet", "HEAD"), environment)
        .strip()
        .removeprefix("refs/heads/")
    )
    if branch != config.branch:
        raise ReleaseError(f"Release requires branch {config.branch}; current branch is {branch}.")
    if git_command(root, ("rev-parse", "--is-shallow-repository"), environment).strip() == "true":
        raise ReleaseError("Release requires a complete, non-shallow repository.")
    if git_command(
        root,
        ("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none"),
        environment,
    ):
        raise ReleaseError(
            "Release requires a clean worktree and index, including untracked files."
        )
    origin = _origin(root, config, environment)
    tags = _local_tags(root, config, environment)
    refs = _remote_refs(root, origin, config, environment)
    remote_branch = _validate_history(root, config, tags, refs, environment)
    latest_tag = max(tags, key=lambda tag: _tag_version(tag.name, config) or (), default=None)
    latest = None if latest_tag is None else latest_tag.name[len(config.tag_prefix) :]
    revision_range = "HEAD"
    if latest_tag is not None:
        git_command(root, ("merge-base", "--is-ancestor", latest_tag.commit_id, head), environment)
        revision_range = f"{latest_tag.commit_id}..{head}"
    output = git_command(
        root,
        ("log", "--no-show-signature", "-z", "--format=%B", revision_range, "--", *config.paths),
        environment,
    )
    messages = tuple(message.strip() for message in output.split("\0") if message.strip())
    if not messages:
        raise ReleaseError("There are no relevant commits to release.")
    ahead = int(
        git_command(root, ("rev-list", "--count", f"{remote_branch}..{head}"), environment).strip()
    )
    return ReleaseState(head, branch, origin, refs, tags, latest, messages, ahead)
