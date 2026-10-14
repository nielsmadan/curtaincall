import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging


def recipe(source: Path, environment: dict[str, str], name: str) -> None:
    result = subprocess.run(
        ("just", name), cwd=source, env=environment, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr


def help_text(directory: Path, environment: dict[str, str]) -> str:
    result = subprocess.run(
        (str(Path(environment["UV_TOOL_BIN_DIR"]) / "curtains"), "--help"),
        cwd=directory,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def change_description(source: Path, description: str) -> None:
    path = source / "src/curtaincall/cli.py"
    contents = path.read_text()
    original = 'description="Prepare and publish a Git release."'
    assert original in contents
    path.write_text(contents.replace(original, f'description="{description}"'))


def test_snapshot_reinstall_editable_and_uninstall(
    package_root: Path, package_source: Path, package_environment: dict[str, str]
) -> None:
    recipe(package_source, package_environment, "install")
    original_help = help_text(package_root, package_environment)
    assert "Prepare and publish a Git release." in original_help
    change_description(package_source, "Installed snapshot refreshed.")
    assert help_text(package_root, package_environment) == original_help
    recipe(package_source, package_environment, "install")
    assert "Installed snapshot refreshed." in help_text(package_root, package_environment)

    recipe(package_source, package_environment, "install-editable")
    path = package_source / "src/curtaincall/cli.py"
    path.write_text(
        path.read_text().replace("Installed snapshot refreshed.", "Editable edit visible.")
    )
    assert "Editable edit visible." in help_text(package_root, package_environment)

    unrelated = package_root / "bin/another-tool"
    unrelated.write_text("preserve me\n")
    recipe(package_source, package_environment, "uninstall")
    assert unrelated.read_text() == "preserve me\n"
    assert not (package_root / "bin/curtains").exists()
    assert not (package_root / "tools/curtaincall").exists()
