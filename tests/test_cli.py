import json
import os
import pty
import subprocess
import sys
from collections.abc import Mapping
from importlib.metadata import version
from pathlib import Path

import pytest

from curtaincall.cli import main
from curtaincall.git import git_command, run_command
from curtaincall.models import Command

pytestmark = pytest.mark.integration


def configure(root: Path, environment: dict[str, str], **policy: object) -> None:
    path = root / "scripts/release.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"name": "CLI Example", "initial_version": "0.1.0", **policy}))
    git_command(root, ("add", "--", "scripts/release.json"), environment)
    git_command(root, ("commit", "-m", "chore: configure release"), environment)


def invoke(
    directory: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (sys.executable, "-m", "curtaincall", *arguments),
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(
    "argument, expected",
    [("--help", "--dry-run"), ("--version", f"curtains {version('curtaincall')}")],
)
def test_help_version_outside_repository(
    tmp_path: Path, git_environment: dict[str, str], argument: str, expected: str
) -> None:
    result = invoke(tmp_path, git_environment, argument)
    assert result.returncode == 0
    assert expected in result.stdout


def test_usage_error(tmp_path: Path, git_environment: dict[str, str]) -> None:
    result = invoke(tmp_path, git_environment, "--unknown")
    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_missing_repository(tmp_path: Path, git_environment: dict[str, str]) -> None:
    result = invoke(tmp_path, git_environment, "--dry-run")
    assert result.returncode == 1
    assert "not a git repository" in result.stderr


@pytest.mark.parametrize("contents", [None, "not json"])
def test_missing_or_invalid_config(
    repository: Path, git_environment: dict[str, str], contents: str | None
) -> None:
    if contents is not None:
        (repository / "scripts").mkdir()
        (repository / "scripts/release.json").write_text(contents)
    result = invoke(repository, git_environment, "--dry-run")
    assert result.returncode == 1
    assert "Cannot read release config" in result.stderr


def test_directory_config_and_inherited_context(
    repository: Path, tmp_path: Path, git_environment: dict[str, str]
) -> None:
    configure(repository, git_environment)
    nested = repository / "nested/deeper"
    nested.mkdir(parents=True)
    environment = {
        **git_environment,
        "GIT_DIR": str(tmp_path / "wrong"),
        "GIT_INDEX_FILE": str(tmp_path / "wrong-index"),
    }
    result = invoke(
        tmp_path,
        environment,
        "--directory",
        str(nested),
        "--config",
        "../../scripts/release.json",
        "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    assert "CLI Example: first release -> v0.1.0" in result.stdout
    assert git_command(repository, ("status", "--porcelain"), git_environment) == ""
    assert git_command(repository, ("tag", "--list"), git_environment) == ""


def test_external_config_rejected(repository: Path, git_environment: dict[str, str]) -> None:
    result = invoke(repository, git_environment, "--config", "../elsewhere.json", "--dry-run")
    assert result.returncode == 1
    assert "inside the selected worktree" in result.stderr


def test_nonterminal_requires_yes(repository: Path, git_environment: dict[str, str]) -> None:
    configure(repository, git_environment)
    result = invoke(repository, git_environment)
    assert result.returncode == 1
    assert "use --yes" in result.stderr
    assert git_command(repository, ("tag", "--list"), git_environment) == ""


@pytest.mark.parametrize("answer", [b"\n", b"\x04"])
def test_real_terminal_cancel_and_eof(
    repository: Path, git_environment: dict[str, str], answer: bytes
) -> None:
    configure(repository, git_environment)
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            (sys.executable, "-m", "curtaincall"),
            cwd=repository,
            env=git_environment,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            os.write(master, answer)
            stdout, stderr = communicate_bounded(process, timeout=15)
            assert process.returncode == 0, stderr
            assert b"Release cancelled." in stdout + stderr
    finally:
        os.close(master)
        os.close(slave)
    assert git_command(repository, ("tag", "--list"), git_environment) == ""


def communicate_bounded(process: subprocess.Popen[bytes], *, timeout: float) -> tuple[bytes, bytes]:
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise


def test_terminal_timeout_reaps_waiting_child(tmp_path: Path) -> None:
    master, slave = pty.openpty()
    try:
        with subprocess.Popen(
            (sys.executable, "-c", "input(); print('first answer consumed', flush=True); input()"),
            cwd=tmp_path,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            os.write(master, b"\n")
            with pytest.raises(subprocess.TimeoutExpired):
                communicate_bounded(process, timeout=1)
            assert process.returncode is not None
            assert process.returncode < 0
    finally:
        os.close(master)
        os.close(slave)


def test_cli_origin_failure_redacts_credentials(
    repository: Path, git_environment: dict[str, str]
) -> None:
    configure(repository, git_environment)
    git_command(
        repository,
        (
            "remote",
            "set-url",
            "origin",
            "https://synthetic-user:synthetic-password@example.invalid/repo.git",
        ),
        git_environment,
    )
    result = invoke(repository, git_environment, "--dry-run")
    assert result.returncode == 1
    assert "ls-remote" in result.stderr
    assert "https://[redacted]@example.invalid/repo.git" in result.stderr
    assert "transport 'https' not allowed" in result.stderr
    assert "synthetic-user" not in result.stdout + result.stderr
    assert "synthetic-password" not in result.stdout + result.stderr


def test_publication_interruption_is_failure_with_preserved_artifacts(
    repository: Path,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure(
        repository,
        git_environment,
        stages=[
            {
                "files": ["tracked.txt"],
                "commands": [
                    [
                        "{python}",
                        "-c",
                        "from pathlib import Path; Path('tracked.txt').write_text('prepared')",
                    ]
                ],
                "message": "chore: release",
            }
        ],
    )
    before = git_command(repository, ("rev-parse", "HEAD"), git_environment).strip()
    pushes = []

    def interrupt_push(
        root: Path,
        argv: Command,
        environment: Mapping[str, str],
        *,
        capture: bool = True,
        input_data: bytes | None = None,
    ) -> str:
        if argv[0] == "git" and "push" in argv:
            pushes.append(argv)
            raise KeyboardInterrupt
        return run_command(root, argv, environment, capture=capture, input_data=input_data)

    monkeypatch.setattr("curtaincall.git.run_command", interrupt_push)
    monkeypatch.setattr(os, "environ", git_environment)
    assert main(("--directory", str(repository), "--yes")) == 1
    diagnostic = capsys.readouterr().err
    assert "Publication failed; local commits and tag are preserved." in diagnostic
    assert "remote outcome may be uncertain: inspect remote refs" in diagnostic
    assert len(pushes) == 1
    prepared = git_command(repository, ("rev-parse", "HEAD"), git_environment).strip()
    assert git_command(repository, ("rev-parse", "HEAD^"), git_environment).strip() == before
    assert git_command(repository, ("cat-file", "-t", "v0.1.0"), git_environment) == "tag\n"
    assert (
        git_command(repository, ("rev-parse", "v0.1.0^{commit}"), git_environment).strip()
        == prepared
    )
    assert git_command(repository, ("show", "v0.1.0:tracked.txt"), git_environment) == "prepared"
    assert git_command(repository, ("status", "--porcelain"), git_environment) == ""


@pytest.mark.parametrize("component", ["bin", ""])
@pytest.mark.parametrize("available", [True, False])
def test_required_tool_path_uses_selected_root(
    repository: Path, git_environment: dict[str, str], component: str, available: bool
) -> None:
    tool = "curtains-test-required-tool"
    source = repository if available else repository.parent
    executable = source / component / tool
    executable.parent.mkdir(exist_ok=True)
    executable.write_text("#!/bin/sh\nprintf 'repository tool ran\\n'\n")
    executable.chmod(0o755)
    if available:
        git_command(
            repository, ("add", "--", str(executable.relative_to(repository))), git_environment
        )
    configure(repository, git_environment, tools=[tool], checks=[[tool]])
    environment = {**git_environment, "PATH": component + os.pathsep + git_environment["PATH"]}
    result = invoke(repository.parent, environment, "--directory", str(repository), "--yes")
    if available:
        assert result.returncode == 0, result.stderr
        assert "repository tool ran" in result.stdout
        assert "Pushed v0.1.0" in result.stdout
    else:
        assert result.returncode == 1
        assert f"Required release tool is unavailable: {tool}" in result.stderr


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_interruption_has_no_traceback(
    repository: Path,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    interruption: type[BaseException],
) -> None:
    configure(repository, git_environment)

    def interrupted(*_: object) -> None:
        raise interruption

    monkeypatch.setattr("curtaincall.cli.release", interrupted)
    monkeypatch.setattr(os, "environ", git_environment)
    assert main(("--directory", str(repository))) == 0
    assert capsys.readouterr().err == "\nRelease cancelled.\n"


def test_cli_success_preparation_and_override(
    repository: Path, git_environment: dict[str, str]
) -> None:
    configure(
        repository,
        git_environment,
        stages=[
            {
                "files": ["tracked.txt"],
                "commands": [
                    [
                        "{python}",
                        "-c",
                        "from pathlib import Path; Path('tracked.txt').write_text('{version}')",
                    ]
                ],
                "checks": [
                    [
                        "{python}",
                        "-c",
                        "from pathlib import Path; assert Path('tracked.txt').read_text() == '{version}'",
                    ]
                ],
                "message": "chore: release {version}",
            }
        ],
    )
    result = invoke(repository, git_environment, "v2.3.4", "--yes")
    assert result.returncode == 0, result.stderr
    assert "Pushed v2.3.4 at" in result.stdout
    assert git_command(repository, ("show", "v2.3.4:tracked.txt"), git_environment) == "2.3.4"
    assert git_command(repository, ("status", "--porcelain"), git_environment) == ""


def test_cli_failure_preserves_preparation(
    repository: Path, git_environment: dict[str, str]
) -> None:
    configure(
        repository,
        git_environment,
        stages=[
            {
                "files": ["tracked.txt"],
                "commands": [
                    [
                        "{python}",
                        "-c",
                        "from pathlib import Path; Path('tracked.txt').write_text('prepared'); raise SystemExit(9)",
                    ]
                ],
                "message": "chore: release",
            }
        ],
    )
    result = invoke(repository, git_environment, "--yes")
    assert result.returncode == 1
    assert "Command failed (9)" in result.stderr
    assert (repository / "tracked.txt").read_text() == "prepared"
    assert git_command(repository, ("tag", "--list"), git_environment) == ""


def test_loaded_policy_is_immutable_during_preparation(
    repository: Path, git_environment: dict[str, str]
) -> None:
    configure(
        repository,
        git_environment,
        stages=[
            {
                "files": ["scripts/release.json"],
                "commands": [
                    [
                        "{python}",
                        "-c",
                        "from pathlib import Path; Path('scripts/release.json').write_text('not valid JSON')",
                    ]
                ],
                "message": "chore: change config",
            }
        ],
    )
    result = invoke(repository, git_environment, "--yes")
    assert result.returncode == 0, result.stderr
    assert "Pushed v0.1.0" in result.stdout
    assert (
        git_command(repository, ("show", "v0.1.0:scripts/release.json"), git_environment)
        == "not valid JSON"
    )


def test_dry_run_cli_skips_custom_commands(
    repository: Path, git_environment: dict[str, str]
) -> None:
    configure(
        repository,
        git_environment,
        checks=[["impossible-command"]],
        tools=["missing-tool"],
        suggest=["also-impossible"],
    )
    result = invoke(repository, git_environment, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Dry run" in result.stdout
    assert git_command(repository, ("tag", "--list"), git_environment) == ""
