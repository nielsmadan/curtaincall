import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from curtaincall import ci

pytestmark = pytest.mark.integration

TAG = "v0.1.0"
REPOSITORY = "o/n"
FAKE_GH = Path(__file__).with_name("fake_gh.py")
WRITES = frozenset({"create", "upload", "delete-asset", "edit"})

type Release = dict[str, object]


@dataclass(frozen=True, slots=True)
class Setup:
    root: Path
    target: str
    notes: Path
    assets: tuple[Path, ...]
    state: Path
    log: Path
    environment: dict[str, str]


def git(root: Path, environment: dict[str, str], *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv), cwd=root, env=environment, check=True, capture_output=True
    )
    return os.fsdecode(result.stdout).strip()


def write_policy(root: Path, **fields: object) -> None:
    path = root / "scripts/release.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    policy = {"name": "CI Example", "initial_version": "0.1.0", "repository": REPOSITORY, **fields}
    path.write_text(json.dumps({key: value for key, value in policy.items() if value is not None}))


def executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path


@pytest.fixture
def setup(
    tmp_path: Path,
    repository: Path,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> Setup:
    remote = tmp_path / "remote.git"
    (repository / "tracked.txt").write_text("release\n")
    git(repository, git_environment, "commit", "-am", "feat: release")
    git(repository, git_environment, "tag", "-a", "-m", TAG, TAG)
    git(repository, git_environment, "push", str(remote), "HEAD:refs/heads/main", TAG)
    git(
        repository,
        git_environment,
        "remote",
        "set-url",
        "origin",
        f"ssh://git@github.com/{REPOSITORY}",
    )
    write_policy(repository)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable(
        bin_directory / "gh",
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))} "$@"\n',
    )
    ssh = executable(tmp_path / "ssh", '#!/bin/sh\nexec git upload-pack "$FAKE_REMOTE"\n')
    files = tmp_path / "files"
    files.mkdir()
    notes = files / "notes.md"
    notes.write_text("## Notes\n\n- change\n")
    assets = (files / "a.tar.gz", files / "b.whl")
    assets[0].write_bytes(b"alpha" * 1000)
    assets[1].write_bytes(b"beta")
    environment = {
        **git_environment,
        "PATH": f"{bin_directory}{os.pathsep}{git_environment['PATH']}",
        "GIT_ALLOW_PROTOCOL": "file:ssh",
        "GIT_SSH_COMMAND": str(ssh),
        "GIT_SSH_VARIANT": "simple",
        "FAKE_REMOTE": str(remote),
        "FAKE_GH_STATE": str(tmp_path / "gh.json"),
        "FAKE_GH_LOG": str(tmp_path / "gh.log"),
    }
    for key, value in environment.items():
        monkeypatch.setenv(key, value)
    return Setup(
        repository,
        git(repository, git_environment, "rev-parse", "HEAD"),
        notes,
        assets,
        tmp_path / "gh.json",
        tmp_path / "gh.log",
        environment,
    )


def run(setup: Setup, *extra: str, assets: tuple[Path, ...] | None = None) -> int:
    files = setup.assets if assets is None else assets
    return ci.main(
        (
            "publish",
            "--directory",
            str(setup.root),
            TAG,
            setup.target,
            "--notes",
            str(setup.notes),
            *extra,
            *(str(path) for path in files),
        )
    )


def calls(setup: Setup) -> list[list[str]]:
    if not setup.log.exists():
        return []
    return [json.loads(line) for line in setup.log.read_text().splitlines()]


def writes(setup: Setup) -> list[list[str]]:
    return [argv for argv in calls(setup) if argv[1] in WRITES]


def release(setup: Setup) -> Release:
    value = json.loads(setup.state.read_text())["release"]
    assert isinstance(value, dict)
    return value


def assets_of(value: Release) -> list[dict[str, object]]:
    found = value["assets"]
    assert isinstance(found, list)
    return found


def store(setup: Setup, value: object) -> None:
    setup.state.write_text(json.dumps({"release": value}))
    setup.log.unlink(missing_ok=True)


def complete(setup: Setup, capsys: pytest.CaptureFixture[str], *extra: str) -> Release:
    assert run(setup, *extra) == 0
    capsys.readouterr()
    setup.log.unlink()
    return release(setup)


def gh(operation: str, *arguments: str) -> list[str]:
    return ["release", operation, "--repo", REPOSITORY, *arguments]


def stored_draft(setup: Setup, capsys: pytest.CaptureFixture[str]) -> Release:
    draft = complete(setup, capsys)
    draft["isDraft"] = True
    return draft


def test_first_publication(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(setup) == 0
    assert writes(setup) == [
        gh(
            "create",
            "--draft",
            "--verify-tag",
            "--title",
            "CI Example 0.1.0",
            "--notes-file",
            str(setup.notes),
            "--",
            TAG,
        ),
        gh("upload", "--", TAG, *(str(path) for path in setup.assets)),
        gh("edit", "--draft=false", "--", TAG),
    ]
    assert capsys.readouterr() == (
        f"Created draft release {TAG}\nUploaded 2 asset(s) to {TAG}\nPublished {TAG}\n",
        "",
    )
    published = release(setup)
    assert published["isDraft"] is False
    assert [asset["state"] for asset in assets_of(published)] == ["uploaded", "uploaded"]


def test_first_prerelease_not_latest(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(setup, "--title", "Custom", "--prerelease", "--not-latest") == 0
    create, _, edit = writes(setup)
    assert create == gh(
        "create",
        "--draft",
        "--verify-tag",
        "--title",
        "Custom",
        "--notes-file",
        str(setup.notes),
        "--prerelease",
        "--",
        TAG,
    )
    assert edit == gh("edit", "--draft=false", "--prerelease", "--latest=false", "--", TAG)
    assert release(setup)["isPrerelease"] is True
    assert capsys.readouterr().out.endswith(f"Published {TAG}\n")


def test_zero_assets(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(setup, assets=()) == 0
    assert [argv[1] for argv in writes(setup)] == ["create", "edit"]
    assert capsys.readouterr().out == f"Created draft release {TAG}\nPublished {TAG}\n"


def test_complete_release_is_left_alone(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    complete(setup, capsys)
    assert run(setup) == 0
    assert writes(setup) == []
    assert capsys.readouterr() == (f"Release {TAG} already complete\n", "")


def test_retry_uploads_only_missing_assets(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    published = complete(setup, capsys)
    published["assets"] = assets_of(published)[:1]
    store(setup, published)
    assert run(setup) == 0
    assert writes(setup) == [gh("upload", "--", TAG, str(setup.assets[1]))]
    assert capsys.readouterr().out == f"Uploaded 1 asset(s) to {TAG}\n"


def test_failed_edit_leaves_draft(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "edit")
    assert run(setup) == 1
    assert (
        capsys.readouterr().err == "curtains: gh release edit failed (1): HTTP 502: Bad Gateway\n"
    )
    assert [argv[1] for argv in writes(setup)] == ["create", "upload", "edit"]
    assert release(setup)["isDraft"] is True


@pytest.mark.parametrize(
    "extra, edit",
    [
        ((), ["release", "edit", "--repo", REPOSITORY, "--draft=false", "--", TAG]),
        (
            ("--not-latest",),
            ["release", "edit", "--repo", REPOSITORY, "--draft=false", "--latest=false", "--", TAG],
        ),
    ],
)
def test_retry_publishes_complete_draft(
    setup: Setup,
    capsys: pytest.CaptureFixture[str],
    extra: tuple[str, ...],
    edit: list[str],
) -> None:
    store(setup, stored_draft(setup, capsys))
    assert run(setup, *extra) == 0
    assert writes(setup) == [edit]
    assert capsys.readouterr().out == f"Published {TAG}\n"


def test_retry_cleans_incomplete_draft_asset(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "upload:b.whl")
    assert run(setup) == 1
    assert [asset["state"] for asset in assets_of(release(setup))] == ["uploaded", "starter"]
    setup.log.unlink()
    capsys.readouterr()
    monkeypatch.delenv("FAKE_GH_FAIL")
    assert run(setup) == 0
    assert writes(setup) == [
        gh("delete-asset", "--yes", "--", TAG, "b.whl"),
        gh("upload", "--", TAG, str(setup.assets[1])),
        gh("edit", "--draft=false", "--", TAG),
    ]
    assert capsys.readouterr().out == (
        f"Removed incomplete asset b.whl\nUploaded 1 asset(s) to {TAG}\nPublished {TAG}\n"
    )


def test_create_failure_after_creation_continues(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "create")
    monkeypatch.setenv("FAKE_GH_FAIL_APPLIED", "1")
    assert run(setup) == 0
    assert [argv[1] for argv in writes(setup)] == ["create", "upload", "edit"]
    assert capsys.readouterr().out == f"Uploaded 2 asset(s) to {TAG}\nPublished {TAG}\n"


def test_create_failure_without_release(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "create")
    assert run(setup) == 1
    assert [argv[1] for argv in writes(setup)] == ["create"]
    assert capsys.readouterr().err == (
        f"curtains: gh release create {TAG} failed (1): HTTP 502: Bad Gateway\n"
    )


def test_create_and_view_failure_keeps_create_error(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_FAIL", "create")
    monkeypatch.setenv("FAKE_GH_VIEW_ERROR_AFTER_WRITE", "HTTP 401: Bad credentials")
    assert run(setup) == 1
    assert capsys.readouterr().err == (
        f"curtains: gh release create {TAG} failed (1): HTTP 502: Bad Gateway; "
        f"re-checking the release also failed: gh release view {TAG} failed (1): "
        "HTTP 401: Bad credentials\n"
    )


def test_release_missing_after_creation(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_CREATE_NOOP", "1")
    assert run(setup) == 1
    assert capsys.readouterr().err == f"curtains: GitHub release {TAG} is missing after creation.\n"


def test_incomplete_upload_keeps_draft(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_UPLOAD_PARTIAL", "1")
    assert run(setup) == 1
    assert capsys.readouterr().err == (
        f"curtains: GitHub release {TAG} is incomplete before publication.\n"
    )
    assert [argv[1] for argv in writes(setup)] == ["create", "upload"]
    assert release(setup)["isDraft"] is True


def test_retry_replaces_incomplete_draft_assets(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = stored_draft(setup, capsys)
    draft["assets"] = [
        {"name": asset["name"], "size": 0, "state": "starter", "digest": None}
        for asset in assets_of(draft)
    ]
    store(setup, draft)
    assert run(setup) == 0
    assert capsys.readouterr().out == (
        "Removed incomplete asset a.tar.gz\nRemoved incomplete asset b.whl\n"
        f"Uploaded 2 asset(s) to {TAG}\nPublished {TAG}\n"
    )
    assert release(setup)["isDraft"] is False


def test_final_check_rejects_edit_that_did_not_publish(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_GH_EDIT_NOOP", "1")
    assert run(setup) == 1
    assert capsys.readouterr().err.endswith(
        f"curtains: GitHub release {TAG} is incomplete after publication.\n"
    )


def test_published_release_with_incomplete_asset_refuses(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    published = complete(setup, capsys)
    assets_of(published)[1]["state"] = "starter"
    store(setup, published)
    assert run(setup) == 1
    assert writes(setup) == []
    assert "incomplete assets: b.whl" in capsys.readouterr().err


def test_draft_policy_leaves_complete_draft(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    write_policy(setup.root, workflow="release.yml", draft=True)
    assert run(setup) == 0
    assert [argv[1] for argv in writes(setup)] == ["create", "upload"]
    assert release(setup)["isDraft"] is True
    assert capsys.readouterr().out == (
        f"Created draft release {TAG}\nUploaded 2 asset(s) to {TAG}\nDraft {TAG} ready for review\n"
    )


def test_draft_policy_accepts_complete_draft(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    write_policy(setup.root, workflow="release.yml", draft=True)
    complete(setup, capsys)
    assert run(setup) == 0
    assert writes(setup) == []
    assert capsys.readouterr().out == f"Draft {TAG} ready for review\n"


def test_stale_draft_with_other_title_refuses(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    draft = stored_draft(setup, capsys)
    draft["name"] = "Stale"
    store(setup, draft)
    assert run(setup) == 1
    assert writes(setup) == []
    assert capsys.readouterr().err == (
        f"curtains: GitHub release name differs from the expected {TAG} publication.\n"
    )


def test_draft_policy_refuses_published_release(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    complete(setup, capsys)
    write_policy(setup.root, workflow="release.yml", draft=True)
    assert run(setup) == 1
    assert writes(setup) == []
    assert "the policy expects a draft" in capsys.readouterr().err


def _set(field: str, value: object) -> Callable[[Release], None]:
    return lambda target: target.__setitem__(field, value)


def _set_asset(field: str, value: object) -> Callable[[Release], None]:
    return lambda target: assets_of(target)[0].__setitem__(field, value)


def _append_asset(name: str) -> Callable[[Release], None]:
    return lambda target: assets_of(target).append(
        {"name": name, "size": 4, "state": "uploaded", "digest": None}
    )


@pytest.mark.parametrize(
    "change, message",
    [
        (_set("name", "Other"), "name differs"),
        (_set("body", "other\n"), "body differs"),
        (_set("isPrerelease", True), "prerelease state differs"),
        (_set("isDraft", "yes"), "draft or immutability state is unavailable"),
        (_set("assets", {}), "asset identity is unavailable"),
        (_set_asset("size", 1), "a.tar.gz' has divergent bytes (size differs)"),
        (_set_asset("digest", "sha256:00"), "a.tar.gz' has divergent bytes (digest differs)"),
        (_set_asset("digest", None), "divergent bytes (digest is unavailable)"),
        (_set_asset("name", 7), "asset identity is unavailable"),
        (_append_asset("extra.txt"), "unexpected assets: extra.txt"),
        (_append_asset("b.whl"), "duplicate asset name 'b.whl'"),
    ],
)
def test_divergent_release_refuses(
    setup: Setup,
    capsys: pytest.CaptureFixture[str],
    change: Callable[[Release], None],
    message: str,
) -> None:
    published = complete(setup, capsys)
    change(published)
    store(setup, published)
    assert run(setup) == 1
    assert writes(setup) == []
    assert message in capsys.readouterr().err


@pytest.mark.parametrize(
    "stdout",
    [
        "[]",
        '"text"',
        "{",
        json.dumps(
            {
                "tagName": TAG,
                "name": 1,
                "body": "",
                "isDraft": False,
                "isImmutable": False,
                "isPrerelease": False,
                "assets": [],
            }
        ),
    ],
)
def test_invalid_view_response(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, stdout: str
) -> None:
    monkeypatch.setenv("FAKE_GH_VIEW_STDOUT", stdout)
    assert run(setup) == 1
    assert writes(setup) == []
    assert capsys.readouterr().err == (
        f"curtains: GitHub returned invalid release metadata for {TAG}.\n"
    )


def test_immutable_release_missing_assets_refuses(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    published = complete(setup, capsys)
    published["isImmutable"] = True
    published["assets"] = assets_of(published)[:1]
    store(setup, published)
    assert run(setup) == 1
    assert writes(setup) == []
    assert "immutable and is missing expected assets" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error, reported",
    [
        (
            "HTTP 401: Bad credentials (https://api.github.com/graphql)",
            "HTTP 401: Bad credentials (https://api.github.com/graphql)",
        ),
        ("HTTP 404: Not Found", "HTTP 404: Not Found"),
        (
            "fatal: https://x-access-token:secret@github.com/o/n unavailable",
            "fatal: https://[redacted]@github.com/o/n unavailable",
        ),
    ],
)
def test_view_error_creates_nothing(
    setup: Setup,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    error: str,
    reported: str,
) -> None:
    monkeypatch.setenv("FAKE_GH_VIEW_ERROR", error)
    assert run(setup) == 1
    assert writes(setup) == []
    assert capsys.readouterr().err == f"curtains: gh release view {TAG} failed (1): {reported}\n"


def _retag(setup: Setup, *options: str, revision: str = "HEAD") -> None:
    remote = setup.environment["FAKE_REMOTE"]
    git(setup.root, setup.environment, "tag", "-f", *options, TAG, revision)
    git(setup.root, setup.environment, "push", "-f", remote, f"refs/tags/{TAG}")


@pytest.mark.parametrize(
    "prepare, message",
    [
        (lambda s: _retag(s, "-a", "-m", TAG, revision="HEAD~1"), "on origin identifies"),
        (_retag, "on origin must be annotated"),
        (
            lambda s: git(
                s.root, s.environment, "push", s.environment["FAKE_REMOTE"], f":refs/tags/{TAG}"
            ),
            f"Release tag {TAG} is not on origin.",
        ),
    ],
)
def test_remote_tag_checked_before_gh(
    setup: Setup,
    capsys: pytest.CaptureFixture[str],
    prepare: Callable[[Setup], object],
    message: str,
) -> None:
    prepare(setup)
    assert run(setup) == 1
    assert calls(setup) == []
    assert message in capsys.readouterr().err


def test_remote_tag_checked_again_before_publication(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    remote = setup.environment["FAKE_REMOTE"]
    hook = ["git", f"--git-dir={remote}", "update-ref", "-d", f"refs/tags/{TAG}"]
    monkeypatch.setenv("FAKE_GH_AFTER_UPLOAD", json.dumps(hook))
    assert run(setup) == 1
    assert [argv[1] for argv in writes(setup)] == ["create", "upload"]
    assert capsys.readouterr().err == f"curtains: Release tag {TAG} is not on origin.\n"
    assert release(setup)["isDraft"] is True


def test_unreachable_origin(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_REMOTE", "https://user:secret@example.invalid/missing.git")
    assert run(setup) == 1
    assert calls(setup) == []
    error = capsys.readouterr().err
    assert error.startswith("curtains: git ls-remote origin failed (128): ")
    assert "'https://[redacted]@example.invalid/missing.git'" in error


@pytest.mark.parametrize(
    "prepare, message",
    [
        (
            lambda s: write_policy(s.root, repository=None),
            "Publishing requires config.repository.",
        ),
        (
            lambda s: git(
                s.root, s.environment, "remote", "set-url", "origin", "ssh://git@github.com/o/other"
            ),
            "Origin must be the GitHub repository o/n.",
        ),
    ],
)
def test_location_checks(
    setup: Setup,
    capsys: pytest.CaptureFixture[str],
    prepare: Callable[[Setup], object],
    message: str,
) -> None:
    prepare(setup)
    assert run(setup) == 1
    assert calls(setup) == []
    assert capsys.readouterr() == ("", f"curtains: {message}\n")


@pytest.mark.parametrize("target", ["HEAD", "abc", "A" * 40, "0" * 39])
def test_rejects_invalid_target(
    setup: Setup, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    assert run(replace(setup, target=target)) == 1
    assert calls(setup) == []
    assert "Release target must be a full commit SHA" in capsys.readouterr().err


def test_rejects_invalid_tag(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    arguments = ("publish", "--directory", str(setup.root), "v0.1", setup.target)
    assert ci.main((*arguments, "--notes", str(setup.notes))) == 1
    assert calls(setup) == []
    assert capsys.readouterr().err == "curtains: Invalid release tag: 'v0.1'\n"


@pytest.mark.parametrize("name", ["a#label.txt", "-x.txt", ".hidden", "sp ace", "a..b", "a."])
def test_rejects_invalid_asset_name(
    setup: Setup, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    path = setup.notes.parent / name
    path.write_text("x")
    assert run(setup, assets=(setup.assets[0], path)) == 1
    assert calls(setup) == []
    assert capsys.readouterr().err == f"curtains: Invalid release asset name: {name!r}\n"


@pytest.mark.parametrize("directory", ["a#b", "build[1]", "w*ld", "q?"])
def test_rejects_gh_syntax_in_asset_path(
    setup: Setup, capsys: pytest.CaptureFixture[str], directory: str
) -> None:
    folder = setup.notes.parent / directory
    folder.mkdir()
    (folder / "x.txt").write_text("x")
    assert run(setup, assets=(folder / "x.txt",)) == 1
    assert calls(setup) == []
    assert "Release asset path must not contain" in capsys.readouterr().err


def test_rejects_duplicate_asset_names(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    other = setup.notes.parent / "other"
    other.mkdir()
    (other / "b.whl").write_text("other")
    assert run(setup, assets=(*setup.assets, other / "b.whl")) == 1
    assert calls(setup) == []
    assert capsys.readouterr().err == "curtains: Release asset names must be unique: b.whl\n"


@pytest.mark.parametrize("name", ["missing.zip", "folder.d"])
def test_rejects_asset_that_is_not_a_file(
    setup: Setup, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    path = setup.notes.parent / name
    if name.endswith(".d"):
        path.mkdir()
    assert run(setup, assets=(path,)) == 1
    assert calls(setup) == []
    assert capsys.readouterr().err == f"curtains: Release asset does not exist: {path}\n"


def test_asset_larger_than_read_chunk(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    large = setup.notes.parent / "large.bin"
    large.write_bytes(bytes(1 << 20) + b"tail")
    assert run(setup, assets=(large,)) == 0
    assert capsys.readouterr().out.endswith(f"Published {TAG}\n")
    assert assets_of(release(setup))[0]["size"] == (1 << 20) + 4


def test_relative_paths_resolve_against_directory(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = setup.root / "dist"
    dist.mkdir()
    shutil.copy(setup.notes, dist / "notes.md")
    shutil.copy(setup.assets[0], dist / "a.tar.gz")
    monkeypatch.chdir(setup.root.parent)
    arguments = ("--directory", setup.root.name, TAG, setup.target, "--notes", "dist/notes.md")
    assert ci.main(("publish", *arguments, "dist/a.tar.gz")) == 0
    create, upload, _ = writes(setup)
    expected = Path.cwd() / setup.root.name / "dist"
    assert create[create.index("--notes-file") + 1] == str(expected / "notes.md")
    assert upload == gh("upload", "--", TAG, str(expected / "a.tar.gz"))
    assert capsys.readouterr().out.endswith(f"Published {TAG}\n")


def test_requires_notes(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        ci.main(("publish", "--directory", str(setup.root), TAG, setup.target))
    assert error.value.code == 2
    assert "the following arguments are required: --notes" in capsys.readouterr().err


def test_rejects_non_utf8_notes(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    setup.notes.write_bytes(b"\xffnotes\n")
    assert run(setup) == 1
    assert calls(setup) == []
    assert capsys.readouterr().err == (
        "curtains: 'utf-8' codec can't decode byte 0xff in position 0: invalid start byte\n"
    )


def test_missing_notes_file(setup: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    setup.notes.unlink()
    assert run(setup) == 1
    assert calls(setup) == []
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.startswith("curtains: [Errno 2] No such file or directory")


def test_missing_gh(
    setup: Setup, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    git_only = setup.root.parent / "git-only"
    git_only.mkdir()
    found = shutil.which("git", path=setup.environment["PATH"])
    assert found is not None
    (git_only / "git").symlink_to(found)
    monkeypatch.setenv("PATH", str(git_only))
    assert run(setup) == 1
    assert capsys.readouterr().err.startswith("curtains: Cannot run gh release view")
