import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts/install_action.py"
BARE_PYTHON = Path(sys.base_prefix) / "bin/python3"


def run_installer(script: Path, destination: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(BARE_PYTHON), "-I", str(script), str(destination)),
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def project() -> dict[str, object]:
    value = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert isinstance(value, dict)
    return value


def project_version() -> str:
    return str(project()["version"])


def source_tree(tmp_path: Path, pyproject: str) -> Path:
    source = tmp_path / "source"
    (source / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, source / "scripts/install_action.py")
    (source / "pyproject.toml").write_text(pyproject)
    return source / "scripts/install_action.py"


PYPROJECT = '[project]\nname = "curtaincall"\nversion = "9.9.9"\n'
SCRIPTS = '[project.scripts]\ncurtains = "curtaincall.cli:main"\n'


def test_install_runs_isolated_launcher(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    decoy = tmp_path / "decoy"
    (decoy / "curtaincall").mkdir(parents=True)
    (decoy / "curtaincall/__init__.py").write_text("raise RuntimeError('decoy imported')\n")
    (decoy / "argparse.py").write_text("raise RuntimeError('decoy argparse imported')\n")
    result = run_installer(SCRIPT, destination, elsewhere)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert [path.name for path in (destination / "bin").iterdir()] == ["curtains"]
    launcher = destination / "bin/curtains"
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o755
    for directory in (destination / "bin", elsewhere):
        (directory / "argparse.py").write_text("raise RuntimeError('decoy argparse imported')\n")
    environment = {**os.environ, "PYTHONPATH": str(decoy)}
    version = subprocess.run(
        (str(launcher), "--version"),
        cwd=elsewhere,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert version.returncode == 0, version.stderr
    assert version.stdout.strip() == f"curtains {project_version()}"
    ci_help = subprocess.run(
        (str(launcher), "ci", "--help"),
        cwd=elsewhere,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ci_help.returncode == 0, ci_help.stderr


def test_reinstall_replaces_stale_metadata(tmp_path: Path) -> None:
    destination = tmp_path / "dest"
    assert run_installer(SCRIPT, destination, tmp_path).returncode == 0
    stale = destination / "site/curtaincall-0.0.1.dist-info"
    stale.mkdir()
    result = run_installer(SCRIPT, destination, tmp_path)
    assert result.returncode == 0, result.stderr
    assert sorted(path.name for path in (destination / "site").iterdir()) == [
        f"curtaincall-{project_version()}.dist-info"
    ]


@pytest.mark.parametrize(
    "pyproject, message",
    [
        (
            '[project]\nname = "curtaincall"\n' + SCRIPTS,
            "pyproject.toml [project].version must be a non-empty string",
        ),
        (PYPROJECT, "pyproject.toml [project.scripts].curtains must be MODULE:FUNCTION"),
        (
            PYPROJECT + '[project.scripts]\ncurtains = "curtaincall.cli"\n',
            "pyproject.toml [project.scripts].curtains must be MODULE:FUNCTION",
        ),
        ("[project\n", "Expected ']' at the end of a table declaration (at line 1, column 9)"),
    ],
)
def test_invalid_pyproject_fails(tmp_path: Path, pyproject: str, message: str) -> None:
    result = run_installer(source_tree(tmp_path, pyproject), tmp_path / "dest", tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "",
        f"install_action: {message}\n",
    )


def test_missing_pyproject_fails(tmp_path: Path) -> None:
    script = source_tree(tmp_path, "")
    (script.parent.parent / "pyproject.toml").unlink()
    result = run_installer(script, tmp_path / "dest", tmp_path)
    assert result.returncode == 1
    assert result.stderr.startswith("install_action: [Errno 2] No such file or directory")


def test_broken_launcher_fails_install(tmp_path: Path) -> None:
    script = source_tree(tmp_path, PYPROJECT + SCRIPTS)
    package = script.parent.parent / "src/curtaincall"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "cli.py").write_text(
        "def main() -> int:\n    print('curtains 0.0.0')\n    return 0\n"
    )
    result = run_installer(script, tmp_path / "dest", tmp_path)
    assert (result.returncode, result.stdout, result.stderr) == (
        1,
        "",
        "install_action: installed launcher failed: curtains 0.0.0\n\n",
    )


@pytest.mark.parametrize("extra", [(), ("other",)])
def test_requires_one_destination(tmp_path: Path, extra: tuple[str, ...]) -> None:
    arguments = (str(BARE_PYTHON), "-I", str(SCRIPT), *((str(tmp_path),) if extra else ()), *extra)
    result = subprocess.run(arguments, capture_output=True, text=True, check=False)
    assert (result.returncode, result.stderr) == (2, "usage: install_action.py DEST\n")


def test_action_python_matches_requires_python() -> None:
    action = (ROOT / "action.yml").read_text(encoding="utf-8")
    pinned = re.findall(r'python-version: "([0-9.]+)"', action)
    assert [f">={version}" for version in pinned] == [project()["requires-python"]]
