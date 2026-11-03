import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from curtaincall import ci
from curtaincall.ci import release_commit
from curtaincall.errors import ReleaseError
from curtaincall.models import ReleaseConfig

pytestmark = pytest.mark.integration


def git(root: Path, environment: dict[str, str], *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv), cwd=root, env=environment, check=True, capture_output=True
    )
    return os.fsdecode(result.stdout).strip()


def commit(root: Path, environment: dict[str, str], message: str = "fix: change") -> str:
    with (root / "tracked.txt").open("a") as stream:
        stream.write(message + "\n")
    git(root, environment, "commit", "-am", message)
    return git(root, environment, "rev-parse", "HEAD")


def tag(
    root: Path, environment: dict[str, str], name: str = "v0.1.0", *, annotated: bool = True
) -> str:
    git(root, environment, "tag", *(("-a", "-m", name) if annotated else ()), name)
    git(root, environment, "push", "origin", "HEAD:refs/heads/main", f"refs/tags/{name}")
    return git(root, environment, "rev-parse", f"refs/tags/{name}^{{commit}}")


def policy(root: Path, **fields: object) -> None:
    path = root / "scripts/release.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": "CI Example", "initial_version": "0.1.0", **fields}))


@pytest.fixture
def released(repository: Path, git_environment: dict[str, str]) -> str:
    commit(repository, git_environment, "feat: release")
    return tag(repository, git_environment)


@pytest.fixture
def environ(monkeypatch: pytest.MonkeyPatch, git_environment: dict[str, str]) -> None:
    monkeypatch.setattr(os, "environ", git_environment)


def test_accepts_pushed_annotated_tag(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    assert release_commit(repository, config, "v0.1.0", None, git_environment) == released
    assert release_commit(repository, config, "v0.1.0", released, git_environment) == released
    assert (
        release_commit(repository, config, "v0.1.0", "refs/tags/v0.1.0", git_environment)
        == released
    )
    tag_object = git(repository, git_environment, "rev-parse", "refs/tags/v0.1.0")
    assert release_commit(repository, config, "v0.1.0", tag_object, git_environment) == released


def test_accepts_retry_after_branch_moved(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    commit(repository, git_environment, "fix: later")
    git(repository, git_environment, "push", "origin", "HEAD:refs/heads/main")
    assert release_commit(repository, config, "v0.1.0", released, git_environment) == released


def test_accepts_branch_named_like_tag(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    git(repository, git_environment, "branch", "v0.1.0", "HEAD~1")
    assert release_commit(repository, config, "v0.1.0", released, git_environment) == released


@pytest.mark.parametrize("origin", ["https://github.com/o/n", "https://github.com/o/n.git"])
def test_accepts_matching_repository(
    repository: Path,
    git_environment: dict[str, str],
    config: ReleaseConfig,
    released: str,
    origin: str,
) -> None:
    git(repository, git_environment, "remote", "set-url", "origin", origin)
    policy_config = replace(config, repository="o/n")
    assert release_commit(repository, policy_config, "v0.1.0", None, git_environment) == released


def test_rejects_missing_origin_when_repository_is_set(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    git(repository, git_environment, "remote", "remove", "origin")
    with pytest.raises(ReleaseError, match="No such remote 'origin'"):
        release_commit(
            repository, replace(config, repository="o/n"), "v0.1.0", None, git_environment
        )


def test_rejects_mismatched_repository(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    git(repository, git_environment, "remote", "set-url", "origin", "https://github.com/o/other")
    with pytest.raises(ReleaseError, match="Origin must be the GitHub repository o/n"):
        release_commit(
            repository, replace(config, repository="o/n"), "v0.1.0", None, git_environment
        )


def test_rejects_lightweight_tag(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    tag(repository, git_environment, annotated=False)
    with pytest.raises(ReleaseError, match="must be an annotated tag, got 'commit'"):
        release_commit(repository, config, "v0.1.0", None, git_environment)


def test_rejects_missing_tag(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    with pytest.raises(ReleaseError, match=r"v0\.1\.0 does not exist locally; fetch tags"):
        release_commit(repository, config, "v0.1.0", None, git_environment)


def test_rejects_event_naming_another_commit(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    parent = git(repository, git_environment, "rev-parse", f"{released}^")
    with pytest.raises(ReleaseError, match=f"does not identify v0.1.0 commit {released}"):
        release_commit(repository, config, "v0.1.0", parent, git_environment)


@pytest.mark.parametrize("event", ["0" * 40, "0" * 64])
def test_rejects_well_formed_unknown_event(
    repository: Path,
    git_environment: dict[str, str],
    config: ReleaseConfig,
    released: str,
    event: str,
) -> None:
    with pytest.raises(ReleaseError, match=f"{event} does not identify v0.1.0 commit {released}"):
        release_commit(repository, config, "v0.1.0", event, git_environment)


@pytest.mark.parametrize(
    "event", ["HEAD~1", "-x", "refs/tags/v0.2.0", "v0.1.0", *("0" * n for n in (39, 41, 63, 65))]
)
def test_rejects_invalid_event(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, event: str
) -> None:
    with pytest.raises(ReleaseError, match="Invalid release event"):
        release_commit(repository, config, "v0.1.0", event, git_environment)


def test_rejects_commit_outside_policy_branch(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig
) -> None:
    git(repository, git_environment, "push", "origin", "HEAD:refs/heads/release")
    commit(repository, git_environment, "feat: unreleased branch")
    released = tag(repository, git_environment)
    assert release_commit(repository, config, "v0.1.0", None, git_environment) == released
    with pytest.raises(ReleaseError, match=f"{released} is not contained in origin/release"):
        release_commit(
            repository, replace(config, branch="release"), "v0.1.0", None, git_environment
        )


def test_rejects_missing_remote_tracking_branch(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    git(repository, git_environment, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(ReleaseError, match="fetch the main branch from origin"):
        release_commit(repository, config, "v0.1.0", released, git_environment)


def test_reports_ancestry_failure(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
) -> None:
    missing = commit(repository, git_environment, "fix: lost")
    commit(repository, git_environment, "fix: tip")
    git(repository, git_environment, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repository / ".git/objects" / missing[:2] / missing[2:]).unlink()
    with pytest.raises(ReleaseError, match=r"git merge-base --is-ancestor failed \(128\): "):
        release_commit(repository, config, "v0.1.0", released, git_environment)


@pytest.mark.parametrize(
    "name, prefix",
    [
        ("rust-v1.0.0", "python-v"),
        ("v1.2", "v"),
        ("v01.2.3", "v"),
        ("v1.2.3rc1", "v"),
        ("v1.2.3\n", "v"),
        ("--help", "v"),
    ],
)
def test_rejects_invalid_tag_before_git(
    tmp_path: Path,
    config: ReleaseConfig,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    prefix: str,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"subprocess.run called with {args}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    with pytest.raises(ReleaseError, match="Invalid release tag"):
        release_commit(tmp_path, replace(config, tag_prefix=prefix), name, None, os.environ)


def test_rejects_shallow_clone(
    repository: Path, git_environment: dict[str, str], config: ReleaseConfig, released: str
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
    with pytest.raises(ReleaseError, match="full-history checkout"):
        release_commit(shallow, config, "v0.1.0", released, git_environment)


@pytest.mark.usefixtures("environ")
def test_cli_validate_tag_prints_release_commit(
    repository: Path,
    git_environment: dict[str, str],
    released: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy(repository / "nested", tag_prefix="v")
    arguments = ("--directory", str(repository), "--config", "nested/scripts/release.json")
    assert ci.main(("validate-tag", *arguments, "v0.1.0", released)) == 0
    assert capsys.readouterr() == (f"{released}\n", "")


@pytest.mark.usefixtures("environ")
def test_cli_reports_errors(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    policy(repository, tag_prefix="python-v")
    assert ci.main(("validate-tag", "--directory", str(repository), "rust-v1.0.0")) == 1
    assert capsys.readouterr() == ("", "curtains: Invalid release tag: 'rust-v1.0.0'\n")


@pytest.mark.usefixtures("environ")
def test_cli_missing_policy(repository: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ci.main(("previous-tag", "--directory", str(repository), "v1.0.0")) == 1
    assert "curtains: Cannot read release config" in capsys.readouterr().err


@pytest.mark.usefixtures("environ")
def test_cli_interrupt(
    repository: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    policy(repository)
    monkeypatch.setattr("curtaincall.ci.release_commit", interrupt)
    assert ci.main(("validate-tag", "--directory", str(repository), "v0.1.0")) == 130
    assert capsys.readouterr() == ("", "curtains: interrupted\n")


def test_cli_requires_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        ci.main(())
    assert error.value.code == 2
    assert "required: COMMAND" in capsys.readouterr().err


@pytest.mark.usefixtures("environ")
@pytest.mark.parametrize(
    "current, expected", [("v0.3.0", "v0.2.5\n"), ("v0.2.5", "v0.2.0\n"), ("v0.1.0", "")]
)
def test_cli_previous_tag(
    repository: Path,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    current: str,
    expected: str,
) -> None:
    for name in ("v0.1.0", "rust-v0.2.9", "v0.2.0", "v0.10", "python-v0.2.8", "v0.2.5"):
        commit(repository, git_environment, f"feat: {name}")
        tag(repository, git_environment, name, annotated=name != "v0.2.5")
    policy(repository)
    monkeypatch.chdir(repository)
    assert ci.main(("previous-tag", current)) == 0
    assert capsys.readouterr() == (expected, "")


@pytest.mark.usefixtures("environ")
def test_cli_previous_tag_rejects_shallow_clone(
    repository: Path, git_environment: dict[str, str], capsys: pytest.CaptureFixture[str]
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
    policy(shallow)
    assert ci.main(("previous-tag", "--directory", str(shallow), "v0.2.0")) == 1
    assert "full-history checkout" in capsys.readouterr().err
