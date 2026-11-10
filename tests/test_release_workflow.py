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


def formula_for(tag: str) -> str:
    template = workflow.FORMULA_TEMPLATE.read_text(encoding="utf-8")
    return workflow.render_formula(template, tag, "0" * 64)


@pytest.mark.parametrize(
    "existing, expected_output, expected_tag",
    [
        (None, "updated", "v1.2.3"),
        ("v1.2.2", "updated", "v1.2.3"),
        ("v1.2.3", "updated", "v1.2.3"),
        ("v1.10.0", "skipped-newer", "v1.10.0"),
    ],
)
def test_publish_formula_never_downgrades_the_tap(
    tmp_path: Path, existing: str | None, expected_output: str, expected_tag: str
) -> None:
    formula = tmp_path / "curtaincall.rb"
    formula.write_text(formula_for(TAG))
    tap_formula = tmp_path / "tap/Formula/curtaincall.rb"
    if existing is not None:
        tap_formula.parent.mkdir(parents=True)
        tap_formula.write_text(formula_for(existing))
    result = subprocess.run(
        (sys.executable, str(SCRIPT), "publish-formula", str(formula), str(tap_formula)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{expected_output}\n"
    assert tap_formula.read_text() == formula_for(expected_tag)


def test_publish_formula_rejects_an_unrecognised_tap_formula(tmp_path: Path) -> None:
    formula = tmp_path / "curtaincall.rb"
    formula.write_text(formula_for(TAG))
    tap_formula = tmp_path / "tap.rb"
    unrecognised = 'class Curtaincall < Formula\n  url "https://example.com/x.tar.gz"\nend\n'
    tap_formula.write_text(unrecognised)
    with pytest.raises(workflow.WorkflowError, match="no unique curtaincall release version"):
        workflow.publish_formula(formula, tap_formula)
    assert tap_formula.read_text() == unrecognised
