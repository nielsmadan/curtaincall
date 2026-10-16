import re
import shutil
import subprocess
import sys
import tomllib
from email.parser import Parser
from pathlib import Path
from zipfile import ZipFile

import pytest
from scripts.prepare_release import prepare_release

from curtaincall.config import load_config
from curtaincall.git import git_command

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("version", ["0.1.0", "2.10.3"])
def test_prepare_updates_only_project_version(tmp_path: Path, version: str) -> None:
    original = '[project]\nname = "example"\nversion = "0.0.1" # retained\n'
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(original)
    prepare_release(tmp_path, version)
    assert manifest.read_text() == original.replace("0.0.1", version)
    assert tomllib.loads(manifest.read_text())["project"]["version"] == version


@pytest.mark.parametrize("version", ["01.0.0", "v1.0.0", "1.0", "1.0.0rc1", "1.0.0\n", "bad"])
def test_invalid_version_preserves_manifest(tmp_path: Path, version: str) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_bytes(b'[project]\nversion = "0.1.0"\n')
    before = manifest.read_bytes()
    with pytest.raises(ValueError, match="canonical"):
        prepare_release(tmp_path, version)
    assert manifest.read_bytes() == before


@pytest.mark.parametrize(
    "source",
    [
        '[project]\nname = "example"\n',
        '[project]\nversion = "0.1.0"\n[tool.example]\nversion = "4.0.0"\n',
        '[project]\nversion = "0.1.0"\n[tool.example]\n  version = "4.0.0"\n',
        '[project]\nversion = "0.1.0"\nversion = "0.2.0"\n',
        '[tool.example]\nversion = "0.1.0"\n',
        'version = "0.1.0"\nproject = "invalid"\n',
    ],
)
def test_invalid_manifest_is_not_written(tmp_path: Path, source: str) -> None:
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text(source)
    before = manifest.read_bytes()
    with pytest.raises(ValueError):
        prepare_release(tmp_path, "1.0.0")
    assert manifest.read_bytes() == before


def test_cli_uses_script_repository_and_reports_errors(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copyfile(ROOT / "scripts/prepare_release.py", scripts / "prepare_release.py")
    manifest = tmp_path / "pyproject.toml"
    manifest.write_text('[project]\nversion = "0.1.0"\n')
    for version, expected in (("invalid", 1), ("0.2.0", 0)):
        result = subprocess.run(
            (sys.executable, str(scripts / "prepare_release.py"), version),
            cwd=scripts,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == expected, result.stdout + result.stderr
        if expected:
            assert "Expected a canonical" in result.stderr
    assert tomllib.loads(manifest.read_text())["project"]["version"] == "0.2.0"


def test_self_policy_targets_existing_release_commands() -> None:
    config = load_config(ROOT / "scripts/release.json")
    assert (config.branch, config.tag_prefix, config.initial_version) == ("main", "v", "1.0.0")
    assert config.repository == "nielsmadan/curtaincall"
    assert config.workflow == "release.yml"
    assert (ROOT / ".github/workflows" / config.workflow).is_file()
    assert config.checks == (("just", "check-static"), ("just", "coverage"))
    assert config.stages[0].checks == (("just", "build"), ("just", "check-package"))
    assert config.stages[0].files == ("pyproject.toml", "uv.lock", "CHANGELOG.md")
    assert config.stages[0].commands == (
        ("{python}", "scripts/prepare_release.py", "{version}"),
        ("uv", "lock"),
        ("just", "changelog", "{version}"),
    )
    justfile = (ROOT / "Justfile").read_text()
    for name in ("check", "check-static", "coverage", "build", "check-package", "changelog"):
        assert re.search(rf"^{re.escape(name)}[: ]", justfile, re.MULTILINE)
    assert "\n    @LEFTHOOK=0 uv run --locked curtains {{args}}\n" in justfile
    lefthook = (ROOT / "lefthook.yml").read_text()
    for line in ("run: just check-static", "run: just coverage", "piped: true"):
        assert f"{line}\n" in lefthook
    assert "git-cliff@2.13.1" in justfile
    assert (ROOT / "cliff.toml").is_file()


@pytest.mark.packaging
def test_self_preparation_builds_selected_version(
    package_source: Path, package_environment: dict[str, str]
) -> None:
    for git_argv in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "Release Tester"),
        ("config", "user.email", "release@example.invalid"),
        ("config", "commit.gpgsign", "false"),
        ("add", "."),
        ("commit", "-m", "feat: initial package"),
    ):
        git_command(package_source, git_argv, package_environment)
    config = load_config(package_source / "scripts/release.json")
    for argv in (*config.stages[0].commands, ("just", "build")):
        command = tuple(arg.format(version="0.2.0", python=sys.executable) for arg in argv)
        result = subprocess.run(
            command,
            cwd=package_source,
            env=package_environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    manifest = tomllib.loads((package_source / "pyproject.toml").read_text())
    lock = tomllib.loads((package_source / "uv.lock").read_text())
    package = next(item for item in lock["package"] if item["name"] == "curtaincall")
    assert manifest["project"]["version"] == package["version"] == "0.2.0"
    assert "## v0.2.0" in (package_source / "CHANGELOG.md").read_text()
    wheel = next((package_source / "dist").glob("*.whl"))
    with ZipFile(wheel) as archive:
        name = next(name for name in archive.namelist() if name.endswith(".dist-info/METADATA"))
        assert Parser().parsestr(archive.read(name).decode())["Version"] == "0.2.0"
