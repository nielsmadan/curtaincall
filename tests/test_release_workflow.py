import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from scripts import release_workflow as workflow

from curtaincall.git import git_command

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/release_workflow.py"
TAG = "v1.2.3"
TITLE = "Curtaincall 1.2.3"


@pytest.fixture
def tagged_repository(
    repository: Path, git_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    monkeypatch.chdir(repository)
    for key, value in git_environment.items():
        monkeypatch.setenv(key, value)
    git_command(repository, ("tag", "-a", TAG, "-m", "Release"), git_environment)
    git_command(repository, ("push", "origin", f"refs/tags/{TAG}"), git_environment)
    return repository, git_command(repository, ("rev-parse", "HEAD"), git_environment).strip()


@pytest.mark.integration
def test_tag_validation_accepts_push_and_manual_retry(
    tagged_repository: tuple[Path, str], git_environment: dict[str, str]
) -> None:
    repository, commit = tagged_repository
    assert workflow.validate_tag(TAG, commit) == commit
    assert workflow.validate_tag(TAG, f"refs/tags/{TAG}") == commit
    git_command(repository, ("commit", "--allow-empty", "-m", "fix: later"), git_environment)
    git_command(repository, ("push", "origin", "HEAD:refs/heads/main"), git_environment)
    git_command(repository, ("branch", TAG), git_environment)
    result = subprocess.run(
        (sys.executable, str(SCRIPT), "validate-tag", TAG),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == commit
    workflow.verify_remote_tag(TAG, commit)
    with pytest.raises(workflow.WorkflowError, match="does not identify"):
        workflow.validate_tag(TAG, "HEAD")


@pytest.mark.integration
@pytest.mark.parametrize("state", ["lightweight", "unmerged", "missing"])
def test_tag_validation_rejects_invalid_release_refs(
    tagged_repository: tuple[Path, str], git_environment: dict[str, str], state: str
) -> None:
    repository, _commit = tagged_repository
    git_command(repository, ("tag", "-d", TAG), git_environment)
    if state == "unmerged":
        git_command(
            repository, ("commit", "--allow-empty", "-m", "feat: unpushed"), git_environment
        )
        git_command(repository, ("tag", "-a", TAG, "-m", "Release"), git_environment)
    elif state == "lightweight":
        git_command(repository, ("tag", TAG), git_environment)
    expected = {"unmerged": "not contained", "lightweight": "annotated", "missing": "failed"}
    with pytest.raises(workflow.WorkflowError, match=expected[state]):
        workflow.validate_tag(TAG)


@pytest.mark.parametrize("tag", ["v1.2", "v01.2.3", "v1.2.3rc1", "v1.2.3\n", "--help"])
def test_invalid_tag_is_rejected_before_running_git(tag: str) -> None:
    with (
        patch.object(workflow, "command") as command,
        pytest.raises(workflow.WorkflowError, match="Invalid release tag"),
    ):
        workflow.validate_tag(tag)
    command.assert_not_called()


@pytest.mark.parametrize(
    "manifest, expected",
    [
        ('[project]\nname="curtaincall"\nversion="1.2.3"\n', "version=1.2.3\n"),
        ('[project]\nname="other"\nversion="1.2.3"\n', "Expected the curtaincall"),
        ('[project]\nname="curtaincall"\nversion="1.2.4"\n', "does not match"),
        ('[project]\nname="curtaincall"\n', "does not match"),
        ("[", "Release workflow stopped"),
    ],
)
def test_manifest_validation_cli(tmp_path: Path, manifest: str, expected: str) -> None:
    project = tmp_path / "pyproject.toml"
    project.write_text(manifest)
    result = subprocess.run(
        (sys.executable, str(SCRIPT), "project-metadata", TAG, str(project)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == (0 if expected.startswith("version=") else 1)
    assert expected in result.stdout + result.stderr


def test_formula_cli_pins_tag_archive_and_digest(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(b"source")
    output = tmp_path / "curtaincall.rb"
    result = subprocess.run(
        (sys.executable, str(SCRIPT), "formula", TAG, str(archive), str(output)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    formula = output.read_text()
    url = "https://github.com/nielsmadan/curtaincall/archive/refs/tags/v1.2.3.tar.gz"
    assert f'  url "{url}"\n' in formula
    assert f'  sha256 "{hashlib.sha256(b"source").hexdigest()}"\n' in formula
    assert re.search(r"@[A-Z0-9]+@", formula) is None


def test_formula_rejects_invalid_tag() -> None:
    with pytest.raises(workflow.WorkflowError, match="Invalid release tag"):
        workflow.render_formula("@VERSION@", "v1.2", "0" * 64)


@pytest.fixture
def publication_files(tmp_path: Path) -> tuple[Path, list[str]]:
    notes = tmp_path / "notes.md"
    notes.write_text("Release notes\n")
    wheel = tmp_path / "curtaincall-1.2.3-py3-none-any.whl"
    source = tmp_path / "curtaincall-1.2.3.tar.gz"
    wheel.write_bytes(b"wheel")
    source.write_bytes(b"source")
    return notes, [str(wheel), str(source)]


def metadata(notes: Path, assets: list[str], *, draft: bool = False) -> dict[str, object]:
    return {
        "tagName": TAG,
        "name": TITLE,
        "body": notes.read_text(),
        "isDraft": draft,
        "isImmutable": False,
        "isPrerelease": False,
        "assets": [
            {
                "name": Path(path).name,
                "size": Path(path).stat().st_size,
                "digest": "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest(),
                "state": "uploaded",
            }
            for path in assets
        ],
    }


def result(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def remote_tag(commit: str) -> subprocess.CompletedProcess[str]:
    return result(stdout=f"{'b' * 40}\trefs/tags/{TAG}\n{commit}\trefs/tags/{TAG}^{{}}\n")


def test_first_publication_verifies_tag_and_creates_release(
    publication_files: tuple[Path, list[str]],
) -> None:
    notes, assets = publication_files
    commit = "a" * 40
    with patch.object(
        subprocess,
        "run",
        side_effect=[remote_tag(commit), result(1, stderr="release not found"), result()],
    ) as runner:
        workflow.publish(TAG, TITLE, notes, commit, assets)
    assert runner.call_args_list[2].args == (
        (
            "gh",
            "release",
            "create",
            TAG,
            *assets,
            "--verify-tag",
            "--title",
            TITLE,
            "--notes-file",
            str(notes),
        ),
    )


@pytest.mark.parametrize("draft", [True, False])
@pytest.mark.parametrize("partial", [True, False])
def test_retry_preserves_matching_assets_and_completes_publication(
    publication_files: tuple[Path, list[str]], draft: bool, partial: bool
) -> None:
    notes, assets = publication_files
    commit = "a" * 40
    release = metadata(notes, assets[:1] if partial else assets, draft=draft)
    responses = [remote_tag(commit), result(stdout=json.dumps(release))]
    responses.extend(result() for _ in range(int(partial) + int(draft)))
    with patch.object(subprocess, "run", side_effect=responses) as runner:
        workflow.publish(TAG, TITLE, notes, commit, assets)
    mutations = [call.args[0] for call in runner.call_args_list[2:]]
    expected = [("gh", "release", "upload", TAG, assets[1])] if partial else []
    if draft:
        expected.append(("gh", "release", "edit", TAG, "--draft=false"))
    assert mutations == expected


@pytest.mark.parametrize("field", ["tagName", "name", "body", "isPrerelease", "isDraft"])
def test_retry_rejects_divergent_metadata_without_writes(
    publication_files: tuple[Path, list[str]], field: str
) -> None:
    notes, assets = publication_files
    release = metadata(notes, assets)
    release[field] = "different"
    with (
        patch.object(
            subprocess,
            "run",
            side_effect=[remote_tag("a" * 40), result(stdout=json.dumps(release))],
        ) as runner,
        pytest.raises(workflow.WorkflowError),
    ):
        workflow.publish(TAG, TITLE, notes, "a" * 40, assets)
    assert runner.call_count == 2


@pytest.mark.parametrize(
    "field, value",
    [
        ("digest", None),
        ("digest", "sha256:wrong"),
        ("size", 0),
        ("state", "new"),
        ("name", "unexpected.whl"),
    ],
)
def test_retry_rejects_divergent_assets_without_writes(
    publication_files: tuple[Path, list[str]], field: str, value: object
) -> None:
    notes, assets = publication_files
    release = metadata(notes, assets)
    remote_assets = release["assets"]
    assert isinstance(remote_assets, list)
    remote_assets[0][field] = value
    with (
        patch.object(
            subprocess,
            "run",
            side_effect=[remote_tag("a" * 40), result(stdout=json.dumps(release))],
        ) as runner,
        pytest.raises(workflow.WorkflowError),
    ):
        workflow.publish(TAG, TITLE, notes, "a" * 40, assets)
    assert runner.call_count == 2


def test_immutable_partial_release_is_rejected(publication_files: tuple[Path, list[str]]) -> None:
    notes, assets = publication_files
    release = metadata(notes, assets[:1])
    release["isImmutable"] = True
    with (
        patch.object(
            subprocess,
            "run",
            side_effect=[remote_tag("a" * 40), result(stdout=json.dumps(release))],
        ) as runner,
        pytest.raises(workflow.WorkflowError, match="immutable"),
    ):
        workflow.publish(TAG, TITLE, notes, "a" * 40, assets)
    assert runner.call_count == 2


@pytest.mark.parametrize("response", [result(1, stderr="HTTP 500"), result(stdout="[]")])
def test_github_lookup_errors_do_not_create_release(
    publication_files: tuple[Path, list[str]], response: subprocess.CompletedProcess[str]
) -> None:
    notes, assets = publication_files
    with (
        patch.object(subprocess, "run", side_effect=[remote_tag("a" * 40), response]) as runner,
        pytest.raises(workflow.WorkflowError),
    ):
        workflow.publish(TAG, TITLE, notes, "a" * 40, assets)
    assert runner.call_count == 2


@pytest.mark.integration
@pytest.mark.parametrize("state", ["moved", "lightweight", "missing"])
def test_remote_tag_change_prevents_publication(
    tagged_repository: tuple[Path, str],
    git_environment: dict[str, str],
    publication_files: tuple[Path, list[str]],
    state: str,
) -> None:
    repository, commit = tagged_repository
    remote = repository.parent / "remote.git"
    git_command(remote, ("tag", "-d", TAG), git_environment)
    if state == "moved":
        git_command(repository, ("commit", "--allow-empty", "-m", "fix: later"), git_environment)
        git_command(repository, ("tag", "-a", "-f", TAG, "-m", "Moved"), git_environment)
        git_command(repository, ("push", "origin", f"refs/tags/{TAG}"), git_environment)
    elif state == "lightweight":
        git_command(remote, ("tag", TAG, commit), git_environment)
    notes, assets = publication_files
    with (
        patch.object(workflow, "_release") as lookup,
        pytest.raises(workflow.WorkflowError),
    ):
        workflow.publish(TAG, TITLE, notes, commit, assets)
    lookup.assert_not_called()
