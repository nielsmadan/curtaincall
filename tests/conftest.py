import json
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from os import devnull, environ
from pathlib import Path

import pytest

from curtaincall.config import load_config
from curtaincall.models import ReleaseConfig
from curtaincall.process import command_environment


@pytest.fixture
def config(tmp_path: Path) -> ReleaseConfig:
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"name": "Example", "initial_version": "0.1.0"}))
    return load_config(path)


def _git_environment() -> dict[str, str]:
    return {
        **command_environment(environ),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": devnull,
        "GIT_CONFIG_SYSTEM": devnull,
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_TERMINAL_PROMPT": "0",
    }


@pytest.fixture
def git_environment() -> dict[str, str]:
    return _git_environment()


def _git(root: Path, environment: dict[str, str], *arguments: str) -> None:
    subprocess.run(("git", *arguments), cwd=root, env=environment, check=True, capture_output=True)


@pytest.fixture(scope="session")
def repository_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    environment = _git_environment()
    template = tmp_path_factory.mktemp("template")
    root = template / "work"
    remote = template / "remote.git"
    root.mkdir()
    _git(root, environment, "init", "--bare", "--initial-branch=main", str(remote))
    for command in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "Release Tester"),
        ("config", "user.email", "release@example.invalid"),
        ("config", "commit.gpgsign", "false"),
        ("config", "tag.gpgsign", "false"),
        ("remote", "add", "origin", str(remote)),
    ):
        _git(root, environment, *command)
    (root / "tracked.txt").write_text("initial\n")
    for command in (
        ("add", "--", "tracked.txt"),
        ("commit", "-m", "feat: initial"),
        ("push", "origin", "HEAD:refs/heads/main"),
    ):
        _git(root, environment, *command)
    return template


@pytest.fixture
def repository(tmp_path: Path, repository_template: Path, git_environment: dict[str, str]) -> Path:
    root = Path(shutil.copytree(repository_template / "work", tmp_path / "work", symlinks=True))
    remote = shutil.copytree(repository_template / "remote.git", tmp_path / "remote.git")
    _git(root, git_environment, "remote", "set-url", "origin", str(remote))
    return root


@pytest.fixture
def package_root() -> Iterator[Path]:
    cache = Path(__file__).resolve().parent.parent / ".cache"
    cache.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="package-", dir=cache) as directory:
        yield Path(directory)


@pytest.fixture
def package_source(package_root: Path) -> Path:
    source = Path(__file__).resolve().parent.parent
    return Path(
        shutil.copytree(
            source,
            package_root / "source",
            ignore=shutil.ignore_patterns(
                ".git",
                ".venv",
                ".uv",
                ".cache",
                "dist",
                "build",
                "__pycache__",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
                ".coverage",
                "htmlcov",
                "coverage.xml",
                "*.egg-info",
            ),
        )
    )


@pytest.fixture
def package_environment(package_root: Path, git_environment: dict[str, str]) -> dict[str, str]:
    return {
        **{
            key: value
            for key, value in git_environment.items()
            if key not in ("PYTHONPATH", "PYTHONHOME") and not key.startswith("UV_")
        },
        "UV_TOOL_DIR": str(package_root / "tools"),
        "UV_TOOL_BIN_DIR": str(package_root / "bin"),
        "UV_CACHE_DIR": str(package_root / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(package_root / "python"),
        "UV_PYTHON": sys.executable,
        "UV_PYTHON_DOWNLOADS": "never",
    }
