import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from curtaincall.config import (
    TEMPLATE_FIELDS,
    load_config,
    load_tag_policy,
    template_values,
    validate_paths,
    validate_ref,
    validate_template,
)
from curtaincall.errors import ReleaseError
from curtaincall.models import ReleaseConfig
from curtaincall.versions import format_version


@pytest.mark.parametrize(
    "path", sorted((Path(__file__).parent / "fixtures/policies").glob("*.json"))
)
def test_existing_policy(path: Path) -> None:
    config = load_config(path)
    original = json.loads(path.read_text())
    assert config.name == original["name"]
    assert format_version(config.initial_version) == original["initial_version"]
    assert len(config.stages) == len(original["stages"])
    assert config.version_parts == original.get("version_parts", 3)


def test_config_is_immutable(config: ReleaseConfig) -> None:
    with pytest.raises(FrozenInstanceError):
        config.name = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "extra,context",
    [
        ({"unknown": 1}, "unknown"),
        ({"components": 3}, "unknown keys: components"),
        ({"paths": []}, "unknown keys: paths"),
        ({"checks": []}, "unknown keys: checks"),
        ({"publication": "done"}, "unknown keys: publication"),
        ({"version_parts": True}, "version_parts"),
        ({"version_parts": 3.0}, "version_parts"),
        ({"version_parts": 4}, "version_parts"),
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
        ({"preflight": "echo hi"}, "preflight"),
        ({"preflight": [[]]}, "preflight"),
        ({"preflight": [[""]]}, "preflight"),
        ({"preflight": [["echo", None]]}, "preflight"),
        ({"preflight": [["echo", "bad\0arg"]]}, "preflight"),
        ({"history_paths": [1]}, "history_paths"),
        ({"success_message": ""}, "success_message"),
        ({"tools": "git"}, "tools"),
        ({"repository": "https://github.com/org/repo"}, "repository"),
        ({"repository": None}, "repository"),
        ({"workflow": "release.yml"}, "workflow"),
        ({"repository": "org/repo", "workflow": "../release.yml"}, "workflow"),
        ({"draft": 1}, "draft"),
        ({"draft": True}, "draft: true requires repository"),
        ({"sign": 1}, "sign"),
        ({"sign": "true"}, "sign"),
        ({"suggest": []}, "suggest"),
        ({"suggest": None}, "suggest"),
        ({"stages": [{}]}, "stages"),
        ({"stages": ["echo"]}, "stages"),
        ({"pins": "tool"}, "pins: expected an array"),
        ({"pins": ["tool", "Tool"]}, r"policy.pins\[1\]: pin names use lowercase"),
        ({"pins": ["tool", "x", "tool"]}, "policy.pins: duplicate pin names: tool$"),
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
    path.write_text(json.dumps({"name": "X", "initial_version": "0.1.0", "history_paths": [value]}))
    with pytest.raises(ReleaseError, match="history_paths"):
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
        "{tag!r}",
        "{tag:>5}",
        "{tag:}",
        "{tag.x}",
        "{tag_prefix[0]}",
        "{tag_prefix:}",
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
                "preflight": [["{literal}", ""]],
                "suggest": ["{literal}"],
                "stages": [
                    {
                        "files": ["dist"],
                        "commands": [
                            ["touch", str(marker)],
                            ["{python}", "{version}", "{{literal}}", "{tag_prefix}"],
                        ],
                        "checks": [["echo", "{revision}", "{tag}", "{tag_prefix}"]],
                        "message": "chore: {tag_prefix}{version} {tag}",
                    }
                ],
            }
        )
    )
    config = load_config(path)
    assert config.preflight == (("{literal}", ""),)
    assert config.stages[0].checks == (("echo", "{revision}", "{tag}", "{tag_prefix}"),)
    assert config.stages[0].commands[1] == ("{python}", "{version}", "{{literal}}", "{tag_prefix}")
    assert config.stages[0].message == "chore: {tag_prefix}{version} {tag}"
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
        ('{"name":"X","name":"Y"}', "policy.name: duplicate"),
        ('{"stages":[{"files":[],"files":[]}]}', r"policy.stages\[0\].files: duplicate"),
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
    validate_paths(root, replace(config, history_paths=("inside", "not-yet-created")))
    (root / "outside").symlink_to(tmp_path)
    with pytest.raises(ReleaseError, match="outside"):
        validate_paths(root, replace(config, history_paths=("outside/data",)))
    (root / "metadata").symlink_to(root / ".git")
    with pytest.raises(ReleaseError, match=r"\.git"):
        validate_paths(root, replace(config, history_paths=("metadata/config",)))


def test_draft_needs_only_a_repository(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps({"name": "X", "initial_version": "0.1.0", "repository": "o/n", "draft": True})
    )
    config = load_config(path)
    assert (config.draft, config.repository, config.workflow) == (True, "o/n", None)


def test_tag_policy_reads_only_tag_and_publication_fields(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps(
            {
                "name": "X",
                "initial_version": "1.0",
                "version_parts": 2,
                "tag_prefix": "x-",
                "repository": "o/n",
                "added_later": True,
                "stages": "not validated here",
            }
        )
    )
    policy = load_tag_policy(path)
    assert (policy.name, policy.version_parts, policy.tag_prefix, policy.repository) == (
        "X",
        2,
        "x-",
        "o/n",
    )
    assert policy.stages == ()


def test_tag_policy_still_validates_tag_fields(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(json.dumps({"name": "X", "initial_version": "1.0.0", "version_parts": 4}))
    with pytest.raises(ReleaseError, match="version_parts"):
        load_tag_policy(path)


def test_template_values_cover_every_template_field(config: ReleaseConfig) -> None:
    values = template_values(config, version=(1, 2, 3), revision="abc", python="py")
    assert tuple(values) == TEMPLATE_FIELDS
    assert values == {
        "version": "1.2.3",
        "revision": "abc",
        "python": "py",
        "tag": "v1.2.3",
        "tag_prefix": "v",
    }


def test_policy_pins(tmp_path: Path) -> None:
    path = tmp_path / "release.json"
    path.write_text(
        json.dumps({"name": "Example", "initial_version": "0.1.0", "pins": ["tool", "other_2"]})
    )
    assert load_config(path).pins == ("tool", "other_2")
