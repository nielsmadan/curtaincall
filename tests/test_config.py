import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from curtaincall.config import load_config, validate_paths, validate_ref, validate_template
from curtaincall.errors import ReleaseError
from curtaincall.models import ReleaseConfig


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "fixtures/policies").glob("*.json"))
)
def test_existing_policy(path: Path) -> None:
    config = load_config(path)
    original = json.loads(path.read_text())
    assert config.name == original["name"]
    assert config.initial_version == original["initial_version"]
    assert len(config.stages) == len(original["stages"])
    assert config.components == original.get("components", 3)


def test_defaults_are_immutable(config: ReleaseConfig) -> None:
    assert config.branch == "main"
    assert config.components == 3
    assert config.tag_prefix == "v"
    assert config.stages == ()
    assert config.paths == ()
    assert config.tools == ()
    assert config.checks == ()
    assert config.publication == "Version tag pushed."
    assert (config.repository, config.workflow, config.suggest, config.draft) == (
        None,
        None,
        None,
        False,
    )
    with pytest.raises(FrozenInstanceError):
        config.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "extra,context",
    [
        ({"unknown": 1}, "unknown"),
        ({"components": True}, "components"),
        ({"components": 3.0}, "components"),
        ({"components": 4}, "components"),
        ({"name": ""}, "name"),
        ({"name": None}, "name"),
        ({"initial_version": "v1.0.0"}, "initial_version"),
        ({"initial_version": "01.0.0"}, "initial_version"),
        ({"tag_prefix": ""}, "tag_prefix"),
        ({"tag_prefix": "bad:"}, "tag_prefix"),
        ({"branch": "-main"}, "branch"),
        ({"branch": "HEAD"}, "branch"),
        ({"branch": "main..next"}, "branch"),
        ({"branch": "foo/.hidden"}, "branch"),
        ({"branch": "foo.lock/bar"}, "branch"),
        ({"checks": "echo hi"}, "checks"),
        ({"checks": [[]]}, "checks"),
        ({"checks": [[""]]}, "checks"),
        ({"checks": [["echo", None]]}, "checks"),
        ({"checks": [["echo", "bad\0arg"]]}, "checks"),
        ({"paths": [1]}, "paths"),
        ({"tools": "git"}, "tools"),
        ({"repository": "https://github.com/org/repo"}, "repository"),
        ({"repository": None}, "repository"),
        ({"workflow": "release.yml"}, "workflow"),
        ({"repository": "org/repo", "workflow": "../release.yml"}, "workflow"),
        ({"draft": 1}, "draft"),
        ({"draft": True}, "draft"),
        ({"suggest": []}, "suggest"),
        ({"suggest": None}, "suggest"),
        ({"stages": [{}]}, "stages"),
        ({"stages": ["echo"]}, "stages"),
    ],
)
def test_invalid_fields(tmp_path: Path, extra: dict[str, object], context: str) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"name": "Example", "initial_version": "0.1.0", **extra}))
    with pytest.raises(ReleaseError, match=context):
        load_config(path)


@pytest.mark.parametrize(
    "value",
    [
        "/absolute/a",
        "../a",
        "a/../b",
        ".",
        "a//b",
        "a/",
        ".git/config",
        "a/.GIT/config",
        ":(top)a",
        "a*",
        "a?",
        "a[0]",
        "a\\b",
    ],
)
def test_rejects_nonliteral_paths(tmp_path: Path, value: str) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"name": "X", "initial_version": "0.1.0", "paths": [value]}))
    with pytest.raises(ReleaseError, match="paths"):
        load_config(path)


@pytest.mark.parametrize(
    "text",
    [
        "{unknown}",
        "{version.attr}",
        "{version[0]}",
        "{version!r}",
        "{version:>5}",
        "{version:}",
        "{}",
        "{",
        "}",
    ],
)
def test_template_fields_are_strict(text: str) -> None:
    with pytest.raises(ReleaseError, match="stage"):
        validate_template(text, "stage")


def test_stage_templates_and_literal_top_level_commands(tmp_path: Path) -> None:
    marker = tmp_path / "marker"
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            {
                "name": "X",
                "initial_version": "0.1.0",
                "checks": [["{literal}", ""]],
                "suggest": ["{literal}"],
                "stages": [
                    {
                        "files": ["dist"],
                        "commands": [
                            ["touch", str(marker)],
                            ["{python}", "{version}", "{{literal}}"],
                        ],
                        "checks": [["echo", "{revision}"]],
                        "message": "chore: {version}",
                    }
                ],
            }
        )
    )
    config = load_config(path)
    assert config.checks == (("{literal}", ""),)
    assert config.stages[0].checks == (("echo", "{revision}"),)
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize(
    "stage",
    [
        {"files": [], "commands": [["echo"]], "message": "release"},
        {"files": ["file"], "commands": [], "message": "release"},
        {"files": ["file"], "commands": [["echo"]], "message": "{no}"},
        {"files": ["file"], "commands": [["echo"]], "message": "release", "oops": True},
    ],
)
def test_invalid_stage(tmp_path: Path, stage: dict[str, object]) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"name": "X", "initial_version": "0.1.0", "stages": [stage]}))
    with pytest.raises(ReleaseError, match="stages"):
        load_config(path)


@pytest.mark.parametrize(
    "text,match",
    [
        ('{"name":"X","name":"Y"}', "config.name: duplicate"),
        ('{"stages":[{"files":[],"files":[]}]}', r"config.stages\[0\].files: duplicate"),
        ("[]", "expected an object"),
        ("{", "Cannot read"),
    ],
)
def test_invalid_json(tmp_path: Path, text: str, match: str) -> None:
    path = tmp_path / "release.json"
    path.write_text(text)
    with pytest.raises(ReleaseError, match=match):
        load_config(path)


def test_missing_config(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="Cannot read"):
        load_config(tmp_path / "missing")


def test_head_is_valid_as_tag_name() -> None:
    validate_ref("HEAD", "tag")


def test_paths_resolve_inside_root(tmp_path: Path, config: ReleaseConfig) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "inside").symlink_to(root / "new-file")
    validate_paths(root, replace(config, paths=("inside", "not-yet-created")))
    (root / "outside").symlink_to(tmp_path)
    with pytest.raises(ReleaseError, match="outside"):
        validate_paths(root, replace(config, paths=("outside/data",)))
    (root / "metadata").symlink_to(root / ".git")
    with pytest.raises(ReleaseError, match=r"\.git"):
        validate_paths(root, replace(config, paths=("metadata/config",)))
