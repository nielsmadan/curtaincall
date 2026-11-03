import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest
from scripts import release_workflow as workflow

SCRIPT = Path(__file__).resolve().parent.parent / "scripts/release_workflow.py"
TAG = "v1.2.3"


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
