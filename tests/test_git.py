import os
import subprocess
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest

from curtaincall.errors import ReleaseError
from curtaincall.git import (
    _local_tags,
    _origin,
    changed_paths,
    command_environment,
    git_command,
    inspect_repository,
    resolve_location,
)
from curtaincall.models import Command, ReleaseConfig
from curtaincall.versions import make_proposal

pytestmark = pytest.mark.integration


def git(root: Path, environment: dict[str, str], *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv), cwd=root, env=environment, check=True, capture_output=True
    )
    return os.fsdecode(result.stdout).strip()


def commit(
    root: Path, environment: dict[str, str], message: str = "fix: change", path: str = "tracked.txt"
) -> str:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    with (root / path).open("a") as stream:
        stream.write(message + "\n")
    git(root, environment, "add", "--", path)
    git(root, environment, "commit", "-m", message)
    return git(root, environment, "rev-parse", "HEAD")


def tag(
    root: Path, environment: dict[str, str], name: str = "v0.1.0", *, annotated: bool = True
) -> None:
    arguments = ("-a", "-m", name) if annotated else ()
    git(root, environment, "tag", *arguments, name)
    git(root, environment, "push", "origin", f"refs/tags/{name}")


def test_initial_inspection(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    before = (repository / ".git/index").read_bytes()
    timestamp = (repository / ".git/index").stat().st_mtime_ns
    os.utime(repository / "tracked.txt", ns=(1_000_000_000, 1_000_000_000))
    state = inspect_repository(repository, config, git_environment)
    assert state.latest is None
    assert state.messages == ("feat: initial",)
    assert state.ahead == 0
    assert state.branch == "main"
    assert state.origin == str(repository.parent / "remote.git")
    assert state.remote_refs == (("refs/heads/main", state.head),)
    assert state.local_tags == ()
    assert (repository / ".git/index").read_bytes() == before
    assert (repository / ".git/index").stat().st_mtime_ns == timestamp
    assert not (repository / ".git/FETCH_HEAD").exists()


@pytest.mark.parametrize("annotated", [True, False])
def test_release_history(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, annotated: bool
) -> None:
    previous = git(repository, git_environment, "rev-parse", "HEAD")
    tag(repository, git_environment, annotated=annotated)
    head = commit(repository, git_environment, "feat: next\n\nBREAKING CHANGE: api")
    state = inspect_repository(repository, config, git_environment)
    assert state.latest == "0.1.0"
    assert state.messages == ("feat: next\n\nBREAKING CHANGE: api",)
    assert state.ahead == 1
    assert state.head == head
    assert state.local_tags[0].name == "v0.1.0"
    assert state.local_tags[0].commit_id == previous
    assert state.local_tags[0].object_id == git(
        repository, git_environment, "rev-parse", "refs/tags/v0.1.0"
    )


def test_numeric_tag_order_and_other_namespaces(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    for name in ("release/1.9.0", "release/1.10.0"):
        tag(repository, git_environment, name)
    for name in ("v9.0.0", "release/1.10.0-rc.1", "release/4.0", "release/01.2.3"):
        git(repository, git_environment, "tag", name)
    commit(repository, git_environment)
    state = inspect_repository(repository, replace(config, tag_prefix="release/"), git_environment)
    assert state.latest == "1.10.0"
    assert {item.name for item in state.local_tags} == {"release/1.9.0", "release/1.10.0"}


def test_numeric_prefix_is_unambiguous(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tag(repository, git_environment, "11.2.3")
    commit(repository, git_environment)
    assert (
        inspect_repository(repository, replace(config, tag_prefix="1"), git_environment).latest
        == "1.2.3"
    )


def test_two_component_tags(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tag(repository, git_environment, "v1.2")
    git(repository, git_environment, "tag", "v3.4.5")
    commit(repository, git_environment)
    state = inspect_repository(
        repository, replace(config, components=2, initial_version="0.1"), git_environment
    )
    assert state.latest == "1.2"
    assert tuple(item.name for item in state.local_tags) == ("v1.2",)


@pytest.mark.parametrize("dirty", ["unstaged", "staged", "untracked", "deleted"])
def test_dirty_tree_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, dirty: str
) -> None:
    target = repository / ("untracked" if dirty == "untracked" else "tracked.txt")
    if dirty == "deleted":
        target.unlink()
    else:
        target.write_text("dirty")
    if dirty == "staged":
        git(repository, git_environment, "add", "--", "tracked.txt")
    with pytest.raises(ReleaseError, match="clean worktree"):
        inspect_repository(repository, config, git_environment)


@pytest.mark.parametrize("mode", ["unborn", "detached", "wrong"])
def test_invalid_branch_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, mode: str
) -> None:
    if mode == "unborn":
        git(repository, git_environment, "checkout", "--orphan", "new")
    elif mode == "detached":
        git(repository, git_environment, "checkout", "--detach")
    else:
        git(repository, git_environment, "checkout", "-b", "other")
    with pytest.raises(ReleaseError):
        inspect_repository(repository, config, git_environment)


def test_shallow_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    shallow = repository.parent / "shallow"
    git(
        repository,
        git_environment,
        "clone",
        "--depth=1",
        (repository.parent / "remote.git").as_uri(),
        str(shallow),
    )
    with pytest.raises(ReleaseError, match="non-shallow"):
        inspect_repository(shallow, config, git_environment)


@pytest.mark.parametrize(
    "mode", ["missing", "missing-destination", "multiple-fetch", "multiple-push", "mismatched-push"]
)
def test_origin_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, mode: str
) -> None:
    if mode == "missing":
        git(repository, git_environment, "remote", "remove", "origin")
    elif mode == "missing-destination":
        git(
            repository,
            git_environment,
            "remote",
            "set-url",
            "origin",
            str(repository.parent / "absent.git"),
        )
    elif mode == "multiple-fetch":
        git(
            repository,
            git_environment,
            "remote",
            "set-url",
            "--add",
            "origin",
            str(repository.parent / "other.git"),
        )
    elif mode == "multiple-push":
        for suffix in ("remote.git", "other.git"):
            git(
                repository,
                git_environment,
                "remote",
                "set-url",
                "--push",
                "--add",
                "origin",
                str(repository.parent / suffix),
            )
    else:
        git(
            repository,
            git_environment,
            "remote",
            "set-url",
            "--push",
            "origin",
            str(repository.parent / "other.git"),
        )
    with pytest.raises(ReleaseError):
        inspect_repository(repository, config, git_environment)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com.evil/owner/repo.git",
        "https://evil/github.com/owner/repo.git",
        "git@github.com:owner/repo.git.evil",
        "https://github.com/owner/repo/extra",
        "https://user@github.com/owner/repo.git",
        "git@github.com:other/repo.git",
    ],
)
def test_repository_identity_rejects_lookalikes(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, url: str
) -> None:
    git(repository, git_environment, "remote", "set-url", "origin", url)
    with pytest.raises(ReleaseError, match="must be the GitHub repository"):
        inspect_repository(repository, replace(config, repository="owner/repo"), git_environment)


@pytest.mark.parametrize(
    "url",
    [
        "git@github.com:owner/repo.git",
        "ssh://git@github.com/owner/repo",
        "https://github.com/owner/repo.git",
    ],
)
def test_repository_identity_accepts_exact_forms_without_network(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, url: str
) -> None:
    git(repository, git_environment, "remote", "set-url", "origin", url)
    assert _origin(repository, replace(config, repository="owner/repo"), git_environment) == url


def test_missing_remote_branch(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    git(repository.parent / "remote.git", git_environment, "update-ref", "-d", "refs/heads/main")
    with pytest.raises(ReleaseError, match="has no main branch"):
        inspect_repository(repository, config, git_environment)


def test_remote_branch_object_missing(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    other = repository.parent / "other"
    git(repository, git_environment, "clone", str(repository.parent / "remote.git"), str(other))
    for key, value in (
        ("user.name", "Tester"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(other, git_environment, "config", key, value)
    commit(other, git_environment)
    git(other, git_environment, "push", "origin", "HEAD:refs/heads/main")
    with pytest.raises(ReleaseError, match="cat-file"):
        inspect_repository(repository, config, git_environment)
    assert not (repository / ".git/FETCH_HEAD").exists()


def test_remote_branch_not_ancestor(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    start = git(repository, git_environment, "rev-parse", "HEAD")
    commit(repository, git_environment, "fix: remote")
    git(repository, git_environment, "push", "origin", "HEAD:refs/heads/main")
    git(repository, git_environment, "reset", "--hard", start)
    commit(repository, git_environment, "fix: local")
    with pytest.raises(ReleaseError, match="merge-base"):
        inspect_repository(repository, config, git_environment)


def test_partial_clone_inspection_does_not_fetch_missing_remote_objects(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    remote = repository.parent / "remote.git"
    partial = repository.parent / "partial"
    git(remote, git_environment, "config", "uploadpack.allowFilter", "true")
    git(
        repository,
        git_environment,
        "clone",
        "--filter=blob:none",
        remote.as_uri(),
        str(partial),
    )
    assert git(partial, git_environment, "config", "remote.origin.promisor") == "true"
    remote_head = commit(repository, git_environment, "fix: remote advance")
    git(repository, git_environment, "push", "origin", "HEAD:refs/heads/main")
    objects = partial / ".git/objects"
    before_objects = {
        path.relative_to(objects): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    }
    before_refs = git(partial, git_environment, "show-ref")
    with pytest.raises(ReleaseError, match="cat-file"):
        inspect_repository(partial, config, git_environment)
    assert {
        path.relative_to(objects): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    } == before_objects
    assert git(partial, git_environment, "show-ref") == before_refs
    git(partial, git_environment, "cat-file", "-e", f"{remote_head}^{{commit}}")
    assert {
        path.relative_to(objects): path.read_bytes()
        for path in objects.rglob("*")
        if path.is_file()
    } != before_objects


@pytest.mark.parametrize("missing", ["local", "remote"])
def test_complete_historic_tag_sets_required(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, missing: str
) -> None:
    tag(repository, git_environment, "v0.1.0")
    commit(repository, git_environment)
    tag(repository, git_environment, "v0.2.0")
    commit(repository, git_environment)
    destination = repository if missing == "local" else repository.parent / "remote.git"
    git(destination, git_environment, "update-ref", "-d", "refs/tags/v0.1.0")
    with pytest.raises(ReleaseError, match="tag sets differ"):
        inspect_repository(repository, config, git_environment)


def test_historic_tag_divergence_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tag(repository, git_environment)
    commit(repository, git_environment)
    tag(repository, git_environment, "v0.2.0")
    commit(repository, git_environment)
    git(repository, git_environment, "tag", "-f", "v0.1.0")
    with pytest.raises(ReleaseError, match=r"v0\.1\.0 has different"):
        inspect_repository(repository, config, git_environment)


def test_latest_tag_must_be_ancestor(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    git(repository, git_environment, "checkout", "-b", "other")
    commit(repository, git_environment)
    tag(repository, git_environment)
    git(repository, git_environment, "checkout", "main")
    with pytest.raises(ReleaseError, match="merge-base"):
        inspect_repository(repository, config, git_environment)


def test_tag_target_must_be_commit(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tree = git(repository, git_environment, "rev-parse", "HEAD^{tree}")
    git(repository, git_environment, "tag", "v0.1.0", tree)
    with pytest.raises(ReleaseError, match="does not resolve to an available commit"):
        inspect_repository(repository, config, git_environment)


def test_signed_history_ignores_signature_display(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    key = repository.parent / "signing-key"
    subprocess.run(
        ("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)),
        env=git_environment,
        check=True,
        capture_output=True,
    )
    signers = repository.parent / "allowed-signers"
    signers.write_text(f"release@example.invalid {key.with_suffix('.pub').read_text()}")
    tag(repository, git_environment, "v1.0.0")
    for setting, value in (
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
        ("gpg.ssh.allowedSignersFile", str(signers)),
        ("commit.gpgsign", "true"),
        ("log.showSignature", "true"),
    ):
        git(repository, git_environment, "config", setting, value)
    commit(repository, git_environment, "feat!: breaking")
    assert "Good" in git(repository, git_environment, "log", "-1", "--format=%B")
    git(repository, git_environment, "config", "commit.gpgsign", "false")
    commit(repository, git_environment, "fix: ordinary")
    state = inspect_repository(repository, config, git_environment)
    assert state.messages == ("fix: ordinary", "feat!: breaking")
    proposal = make_proposal(config, state, None, None, False)
    assert proposal.version == "2.0.0"
    assert (proposal.counts.features, proposal.counts.fixes, proposal.counts.breaking) == (1, 1, 1)


def test_historical_tags_are_peeled_in_one_batch(
    repository: Path,
    git_environment: dict[str, str],
    config: ReleaseConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = git(repository, git_environment, "rev-parse", "HEAD")
    git(repository, git_environment, "tag", "-a", "-m", "annotated", "v0.1.0")
    git(repository, git_environment, "tag", "-a", "-m", "nested", "v0.2.0", "v0.1.0")
    refs = "".join(f"create refs/tags/v0.3.{index} {head}\n" for index in range(30))
    git_command(repository, ("update-ref", "--stdin"), git_environment, input_data=refs.encode())
    calls = []

    def record(
        root: Path,
        argv: Command,
        environment: Mapping[str, str],
        *,
        capture: bool = True,
        input_data: bytes | None = None,
    ) -> str:
        calls.append(argv[0])
        return git_command(root, argv, environment, capture=capture, input_data=input_data)

    monkeypatch.setattr("curtaincall.git.git_command", record)
    tags = _local_tags(repository, config, git_environment)
    assert len(tags) == 32
    assert {tag.commit_id for tag in tags} == {head}
    assert next(tag for tag in tags if tag.name == "v0.1.0").object_id != head
    assert next(tag for tag in tags if tag.name == "v0.2.0").object_id != head
    assert calls == ["for-each-ref", "cat-file"]


@pytest.mark.parametrize("target", ["tree", "blob", "missing"])
def test_tag_batch_rejects_unavailable_commit(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, target: str
) -> None:
    expression = {"tree": "HEAD^{tree}", "blob": "HEAD:tracked.txt", "missing": "HEAD"}[target]
    oid = git(repository, git_environment, "rev-parse", expression)
    git(repository, git_environment, "tag", "v0.1.0", oid)
    if target == "missing":
        (repository / ".git/objects" / oid[:2] / oid[2:]).unlink()
    with pytest.raises(ReleaseError, match=r"v0\.1\.0 does not resolve to an available commit"):
        _local_tags(repository, config, git_environment)


@pytest.mark.parametrize("output", ["", "bad commit\n", "a" * 40 + " tree\n"])
def test_tag_batch_rejects_invalid_response(
    repository: Path,
    git_environment: dict[str, str],
    config: ReleaseConfig,
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    git(repository, git_environment, "tag", "v0.1.0")

    def batch(root: Path, argv: Command, environment: Mapping[str, str], **_kwargs: object) -> str:
        return output if argv[0] == "cat-file" else git_command(root, argv, environment)

    monkeypatch.setattr("curtaincall.git.git_command", batch)
    with pytest.raises(ReleaseError, match=r"incomplete cat-file|available commit"):
        _local_tags(repository, config, git_environment)


def test_scoped_history_and_unscoped_ahead(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tag(repository, git_environment)
    commit(repository, git_environment, "feat: scoped", "src/path with spaces.txt")
    commit(repository, git_environment, "fix: elsewhere", "other.txt")
    state = inspect_repository(repository, replace(config, paths=("src",)), git_environment)
    assert state.messages == ("feat: scoped",)
    assert state.ahead == 2


@pytest.mark.parametrize("released", [True, False])
def test_no_relevant_history_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: bool
) -> None:
    if released:
        tag(repository, git_environment)
    with pytest.raises(ReleaseError, match="no relevant commits"):
        inspect_repository(repository, replace(config, paths=("missing",)), git_environment)


def test_no_commits_since_release_rejected(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tag(repository, git_environment)
    with pytest.raises(ReleaseError, match="no relevant commits"):
        inspect_repository(repository, config, git_environment)


def test_changed_paths_preserve_all_filename_bytes(
    repository: Path, git_environment: dict[str, str]
) -> None:
    names = (
        "space name",
        "new\nline",
        "carriage\rreturn",
        "both\r\nline",
        ":(glob)*",
        "-dash",
    )
    for name in names:
        (repository / name).write_bytes(b"data")
    git(repository, git_environment, "--literal-pathspecs", "add", "--", names[0], names[1])
    (repository / "tracked.txt").rename(repository / "renamed")
    assert changed_paths(repository, git_environment) == frozenset(
        (*names, "tracked.txt", "renamed")
    )


def test_staged_rename_reports_both_paths(
    repository: Path, git_environment: dict[str, str]
) -> None:
    git(repository, git_environment, "mv", "tracked.txt", "renamed.txt")
    assert changed_paths(repository, git_environment) == frozenset({"tracked.txt", "renamed.txt"})


def test_root_and_relative_config_resolution(
    repository: Path, git_environment: dict[str, str]
) -> None:
    nested = repository / "nested/deeper"
    nested.mkdir(parents=True)
    location = resolve_location(nested, None, git_environment)
    assert location.root == repository
    assert location.config_path == repository / "scripts/release.json"
    assert (
        resolve_location(nested, Path("../policy.json"), git_environment).config_path
        == repository / "nested/policy.json"
    )


def test_config_escape_and_symlink_rejected(
    repository: Path, git_environment: dict[str, str]
) -> None:
    (repository / "link").symlink_to(repository.parent, target_is_directory=True)
    for path in (Path("../release.json"), Path("link/release.json"), Path(".git/config")):
        with pytest.raises(ReleaseError, match="inside the selected worktree"):
            resolve_location(repository, path, git_environment)


def test_worktree_root_resolution(repository: Path, git_environment: dict[str, str]) -> None:
    worktree = repository.parent / "linked"
    git(repository, git_environment, "worktree", "add", "-b", "linked", str(worktree))
    assert resolve_location(worktree, None, git_environment).root == worktree


@pytest.mark.parametrize("metadata", [".git", ".GIT", ".GiT"])
@pytest.mark.parametrize("symlink", [False, True])
def test_config_metadata_exclusion_ignores_case(
    repository: Path, git_environment: dict[str, str], metadata: str, symlink: bool
) -> None:
    directory = repository / metadata
    directory.mkdir(exist_ok=True)
    (directory / "release.json").write_text("{}")
    selected = Path(metadata) / "release.json"
    if symlink:
        (repository / "policy.json").symlink_to(selected)
        selected = Path("policy.json")
    with pytest.raises(ReleaseError, match="inside the selected worktree"):
        resolve_location(repository, selected, git_environment)


def test_root_with_newlines_preserved(repository: Path, git_environment: dict[str, str]) -> None:
    renamed = repository.with_name("root\r\n")
    repository.rename(renamed)
    assert resolve_location(renamed, None, git_environment).root == renamed


def test_not_a_repository(tmp_path: Path, git_environment: dict[str, str]) -> None:
    with pytest.raises(ReleaseError, match="Command failed"):
        resolve_location(tmp_path, None, git_environment)


def test_inherited_git_context_cannot_redirect_selection(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    poisoned = {
        **git_environment,
        "GIT_DIR": str(repository.parent / "remote.git"),
        "GIT_WORK_TREE": str(repository.parent),
        "GIT_INDEX_FILE": str(repository.parent / "bad-index"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "remote.origin.url",
        "GIT_CONFIG_VALUE_0": "unreachable",
        "GIT_NAMESPACE": "other",
    }
    environment = command_environment(poisoned)
    location = resolve_location(repository, None, environment)
    assert location.root == repository
    assert inspect_repository(repository, config, environment).messages == ("feat: initial",)


def test_replacement_objects_ignored(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    previous = git(repository, git_environment, "rev-parse", "HEAD")
    tag(repository, git_environment)
    head = commit(repository, git_environment, "fix: actual")
    tree = git(repository, git_environment, "rev-parse", "HEAD^{tree}")
    replacement = git(
        repository, git_environment, "commit-tree", tree, "-p", previous, "-m", "feat!: replacement"
    )
    git(repository, git_environment, "replace", head, replacement)
    assert git(repository, git_environment, "log", "-1", "--format=%s") == "feat!: replacement"
    assert inspect_repository(repository, config, git_environment).messages == ("fix: actual",)


def test_replacement_cannot_fake_ancestry(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    start = git(repository, git_environment, "rev-parse", "HEAD")
    remote_head = commit(repository, git_environment, "fix: remote")
    git(repository, git_environment, "push", "origin", "HEAD:refs/heads/main")
    git(repository, git_environment, "reset", "--hard", start)
    head = commit(repository, git_environment, "fix: local")
    tree = git(repository, git_environment, "rev-parse", "HEAD^{tree}")
    replacement = git(
        repository, git_environment, "commit-tree", tree, "-p", remote_head, "-m", "fake ancestry"
    )
    git(repository, git_environment, "replace", head, replacement)
    git(repository, git_environment, "merge-base", "--is-ancestor", remote_head, head)
    with pytest.raises(ReleaseError, match="merge-base"):
        inspect_repository(repository, config, git_environment)


def test_changed_paths_preserve_undecodable_index_name(
    repository: Path, git_environment: dict[str, str]
) -> None:
    name = os.fsdecode(b"undecodable\xff")
    blob = git(repository, git_environment, "rev-parse", "HEAD:tracked.txt")
    git(
        repository, git_environment, "update-index", "--add", "--cacheinfo", f"100644,{blob},{name}"
    )
    assert changed_paths(repository, git_environment) == frozenset({name})


def test_unrelated_refs_do_not_change_snapshot(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    before = inspect_repository(repository, config, git_environment)
    git(repository, git_environment, "tag", "unrelated")
    git(
        repository,
        git_environment,
        "push",
        "origin",
        "HEAD:refs/heads/other",
        "refs/tags/unrelated",
    )
    after = inspect_repository(repository, config, git_environment)
    assert after == before


def test_branch_name_matching_unrelated_tag(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    git(repository, git_environment, "tag", "main")
    assert inspect_repository(repository, config, git_environment).branch == "main"


def test_push_url_rewriting_cannot_redirect_origin(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    remote = str(repository.parent / "remote.git")
    other = str(repository.parent / "other.git")
    git(repository, git_environment, "config", f"url.{other}.pushInsteadOf", remote)
    with pytest.raises(ReleaseError, match="matching fetch and push destination"):
        inspect_repository(repository, config, git_environment)


def test_inspection_rejects_escaping_configured_symlink(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    (repository / "escape").symlink_to(repository.parent, target_is_directory=True)
    with pytest.raises(ReleaseError, match="outside the worktree"):
        inspect_repository(repository, replace(config, paths=("escape",)), git_environment)


def test_changed_paths_ignore_replacement_tree(
    repository: Path, git_environment: dict[str, str]
) -> None:
    head = git(repository, git_environment, "rev-parse", "HEAD")
    changed = commit(repository, git_environment)
    git(repository, git_environment, "reset", "--hard", head)
    git(repository, git_environment, "replace", head, changed)
    assert git(repository, git_environment, "diff", "--cached", "--name-only") == "tracked.txt"
    assert changed_paths(repository, git_environment) == frozenset()
