import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

import pytest
from scripts import doctor


def available(name: str, _path: str) -> str | None:
    return f"/tools/{name}"


def healthy(argv: Sequence[str]) -> str | None:
    if argv[-1] == "--version":
        return "git version 2.45.0"
    if "rev-parse" in argv:
        return f".git/{argv[-1]}"
    return ""


def inspect(
    root: Path, *, which: doctor.Which = available, run: doctor.Run = healthy
) -> tuple[doctor.Check, ...]:
    return doctor.inspect(root, {}, which=which, run=run)


@pytest.fixture
def healthy_checkout(tmp_path: Path) -> Path:
    for name in ("python", "pytest", "ruff", "pylint", "mypy"):
        path = tmp_path / ".venv/bin" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    for name in ("pre-commit", "pre-push"):
        path = tmp_path / ".git/hooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    (tmp_path / "lefthook.yml").write_text("pre-commit: {}\npre-push: {}\n")
    return tmp_path


def test_healthy_checkout(healthy_checkout: Path) -> None:
    checks = inspect(healthy_checkout)
    assert all(check.ready for check in checks)
    assert {check.name for check in checks} == {
        "git",
        "uv",
        "just",
        "lefthook",
        "Git >=2.45",
        ".venv",
        "pytest",
        "ruff",
        "pylint",
        "mypy",
        "Lefthook configuration",
        "pre-commit",
        "pre-push",
    }


@pytest.mark.parametrize(
    "path, name",
    [
        (".venv/bin/python", ".venv"),
        (".venv/bin/ruff", "ruff"),
        ("lefthook.yml", "Lefthook configuration"),
        (".git/hooks/pre-commit", "pre-commit"),
        (".git/hooks/pre-push", "pre-push"),
    ],
)
def test_missing_checkout_wiring(healthy_checkout: Path, path: str, name: str) -> None:
    (healthy_checkout / path).unlink()
    check = next(check for check in inspect(healthy_checkout) if check.name == name)
    assert check.ready is False
    assert check.hint


def test_hook_must_be_executable(healthy_checkout: Path) -> None:
    (healthy_checkout / ".git/hooks/pre-push").chmod(0o644)
    check = next(check for check in inspect(healthy_checkout) if check.name == "pre-push")
    assert check.ready is False


def test_custom_absolute_hooks_path(healthy_checkout: Path) -> None:
    def absolute(argv: Sequence[str]) -> str | None:
        value = healthy(argv)
        return str(healthy_checkout / value) if value and "rev-parse" in argv else value

    assert all(check.ready for check in inspect(healthy_checkout, run=absolute))


@pytest.mark.parametrize("tool", ["git", "uv", "just", "lefthook"])
def test_missing_tools(healthy_checkout: Path, tool: str) -> None:
    checks = inspect(
        healthy_checkout, which=lambda name, path: None if name == tool else available(name, path)
    )
    check = next(check for check in checks if check.name == tool)
    assert check.ready is False
    assert "install" in check.hint


@pytest.mark.parametrize(
    "value, ready",
    [
        ("git version 2.45.0", True),
        ("git version 2.54.0 (Apple Git-157)", True),
        ("git version 3.0.0", True),
        ("git version 2.44.9", False),
        ("unknown", False),
        (None, False),
    ],
)
def test_git_version(value: str | None, ready: bool) -> None:
    assert doctor.git_version_ready(value) is ready


@pytest.mark.parametrize(
    "argument, name",
    [
        ("--version", "Git >=2.45"),
        ("validate", "Lefthook configuration"),
        ("import sys; raise SystemExit(sys.version_info < (3, 13))", ".venv"),
        ("hooks/pre-push", "pre-push"),
    ],
)
def test_failed_tool_check(healthy_checkout: Path, argument: str, name: str) -> None:
    checks = inspect(
        healthy_checkout, run=lambda argv: None if argv[-1] == argument else healthy(argv)
    )
    check = next(check for check in checks if check.name == name)
    assert check.ready is False


def test_command_success_and_failure(tmp_path: Path) -> None:
    assert doctor.command(tmp_path, os.environ, (sys.executable, "-c", "print('ready')")) == "ready"
    assert (
        doctor.command(tmp_path, os.environ, (sys.executable, "-c", "raise SystemExit(1)")) is None
    )
    assert doctor.command(tmp_path, os.environ, (str(tmp_path / "missing-tool"),)) is None


@pytest.mark.parametrize("component", ["bin", ""])
def test_tools_resolve_relative_path_from_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, component: str
) -> None:
    root = tmp_path / "checkout"
    directory = root / component
    directory.mkdir(parents=True)
    (root / "lefthook.yml").write_text("pre-commit: {}\n")
    for name in ("git", "uv", "just", "lefthook"):
        path = directory / name
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
    monkeypatch.chdir(tmp_path)
    commands = []

    def run(argv: Sequence[str]) -> str:
        commands.append(argv[0])
        return "git version 2.45.0" if argv[-1] == "--version" else ""

    checks = doctor.inspect(
        root,
        {"PATH": component},
        which=lambda name, path: shutil.which(name, path=path),
        run=run,
    )
    assert all(check.ready for check in checks if check.name in {"git", "uv", "just", "lefthook"})
    assert commands == [
        str(directory / "git"),
        str(directory / "lefthook"),
        str(directory / "git"),
        str(directory / "git"),
    ]


@pytest.mark.parametrize(
    "ready, expected", [(True, "Everything in place."), (False, "run: just setup")]
)
def test_main_reports_exit_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ready: bool, expected: str
) -> None:
    monkeypatch.setattr(
        doctor,
        "inspect",
        lambda root, environment, which, run: (doctor.Check("hooks", ready, "run: just setup"),),
    )
    assert doctor.main() == (0 if ready else 1)
    assert expected in capsys.readouterr().out
