import configparser
import json
import subprocess
import tarfile
import zipfile
from email.parser import Parser
from importlib.metadata import version
from pathlib import Path

import pytest

from curtaincall.git import git_command

pytestmark = pytest.mark.packaging


@pytest.fixture
def distributions(package_source: Path, package_environment: dict[str, str]) -> tuple[Path, Path]:
    result = subprocess.run(
        ("just", "build"),
        cwd=package_source,
        env=package_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sdists = tuple((package_source / "dist").glob("*.tar.gz"))
    wheels = tuple((package_source / "dist").glob("*.whl"))
    assert len(sdists) == len(wheels) == 1
    return wheels[0], sdists[0]


def test_distribution_contents(distributions: tuple[Path, Path], package_source: Path) -> None:
    wheel, sdist = distributions
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert all(name.startswith("curtaincall/") or ".dist-info/" in name for name in names)
        metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(archive.read(metadata_name).decode())
        assert metadata["Name"] == "curtaincall"
        assert metadata["Version"] == version("curtaincall")
        assert metadata["Requires-Python"] == ">=3.13"
        assert metadata.get_all("Requires-Dist", []) == []
        entry_name = next(name for name in names if name.endswith(".dist-info/entry_points.txt"))
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(entry_name).decode())
        assert dict(entries["console_scripts"]) == {"curtains": "curtaincall.cli:main"}
    with tarfile.open(sdist) as archive:
        files = {Path(*Path(name).parts[1:]).as_posix() for name in archive.getnames()}
        expected = {
            "pyproject.toml",
            "uv.lock",
            "Justfile",
            "lefthook.yml",
            ".python-version",
            "LICENSE",
            "src/curtaincall/cli.py",
            "scripts/doctor.py",
            "scripts/prepare_release.py",
            "scripts/release_workflow.py",
            "scripts/curtaincall.rb.in",
            "scripts/release.json",
            ".github/workflows/release.yml",
            "README.md",
            "AGENTS.md",
            "CHANGELOG.md",
            "cliff.toml",
            "docs/overview.md",
            "docs/configuration.md",
            "docs/architecture.md",
            "docs/development.md",
            "docs/release.md",
            "docs/migration.md",
            "tests/test_packaging.py",
        }
        expected.update(
            path.relative_to(package_source).as_posix()
            for path in (package_source / "tests/fixtures/policies").glob("*.json")
        )
        expected.update(
            path.relative_to(package_source).as_posix()
            for path in (package_source / "docs").rglob("*.md")
            if "plans" not in path.parts and not path.name.startswith("longshot")
        )
        assert expected <= files
        assert all("docs/plans/" not in name for name in files)


@pytest.mark.parametrize("archive_index", [0, 1], ids=["wheel", "sdist"])
def test_installed_distribution_cli(
    distributions: tuple[Path, Path],
    archive_index: int,
    package_root: Path,
    package_environment: dict[str, str],
    repository: Path,
) -> None:
    result = subprocess.run(
        ("uv", "tool", "install", "--reinstall", "--force", str(distributions[archive_index])),
        cwd=package_root,
        env=package_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    executable = package_root / "bin/curtains"
    for arguments, expected in (
        (("--help",), "--dry-run"),
        (("--version",), f"curtains {version('curtaincall')}"),
    ):
        result = subprocess.run(
            (str(executable), *arguments),
            cwd=package_root,
            env=package_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert expected in result.stdout
    config = repository / "scripts/release.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"name": "Installed CLI", "initial_version": "0.1.0"}))
    git_command(repository, ("add", "scripts/release.json"), package_environment)
    git_command(repository, ("commit", "-m", "chore: configure"), package_environment)
    before = git_command(repository, ("show-ref",), package_environment)
    result = subprocess.run(
        (str(executable), "--directory", str(repository), "--dry-run"),
        cwd=package_root,
        env=package_environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Installed CLI: first release -> v0.1.0" in result.stdout
    assert git_command(repository, ("show-ref",), package_environment) == before
    assert git_command(repository, ("status", "--porcelain"), package_environment) == ""
