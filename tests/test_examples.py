import sys
from dataclasses import replace
from pathlib import Path

import pytest

from curtaincall.config import load_config, validate_paths
from curtaincall.git import git_command, inspect_repository
from curtaincall.models import Location, ReleaseRequest, Stage
from curtaincall.release import release

POLICIES = Path(__file__).parent / "fixtures/policies"


@pytest.mark.parametrize("path", sorted(POLICIES.glob("*.json")), ids=lambda path: path.stem)
def test_standalone_policy_paths(path: Path, tmp_path: Path) -> None:
    config = load_config(path)
    validate_paths(tmp_path, config)
    assert config.name
    assert config.publication


def _synthetic_stage(stage: Stage) -> Stage:
    targets = tuple("dist/example.txt" if path == "dist" else path for path in stage.files)
    code = "from pathlib import Path; " + "; ".join(
        f"Path({path!r}).parent.mkdir(parents=True, exist_ok=True); "
        f"Path({path!r}).write_text('{{version}}:{{revision}}')"
        for path in targets
    )
    check = "from pathlib import Path; " + "; ".join(
        f"assert Path({path!r}).read_text() == '{{version}}:{{revision}}'" for path in targets
    )
    return replace(stage, commands=(("{python}", "-c", code),), checks=(("{python}", "-c", check),))


@pytest.mark.integration
@pytest.mark.parametrize(
    "name, expected",
    [
        ("ShortcutKit", "0.1.1"),
        ("harlequin", "0.2"),
        ("flyrail-python", "0.1.1"),
        ("juggler", "0.1.1"),
        ("mouthfeel", "0.9.1"),
        ("loadout", "0.2.0"),
    ],
)
def test_policy_dimensions_with_synthetic_tools(
    name: str, expected: str, repository: Path, git_environment: dict[str, str]
) -> None:
    original = load_config(POLICIES / f"{name}.json")
    config = replace(
        original,
        repository=None,
        tools=(),
        checks=((sys.executable, "-c", "print('synthetic preflight passed')"),),
        stages=tuple(_synthetic_stage(stage) for stage in original.stages),
        suggest=(sys.executable, "-c", f"print({original.tag_prefix + expected!r})")
        if original.suggest
        else None,
    )
    if config.branch != "main":
        git_command(repository, ("branch", "-m", config.branch), git_environment)
        git_command(
            repository, ("push", "origin", f"HEAD:refs/heads/{config.branch}"), git_environment
        )
    old_tag = config.tag_prefix + config.initial_version
    git_command(repository, ("tag", "--annotate", "-m", "previous", old_tag), git_environment)
    git_command(repository, ("push", "origin", f"refs/tags/{old_tag}"), git_environment)
    relevant = repository / (config.paths[0] if config.paths else "changes") / "example.txt"
    relevant.parent.mkdir(parents=True)
    relevant.write_text("synthetic consumer change")
    git_command(repository, ("add", "--", str(relevant.relative_to(repository))), git_environment)
    git_command(repository, ("commit", "-m", "fix: synthetic change"), git_environment)
    before = inspect_repository(repository, config, git_environment)
    release(
        Location(repository, repository / "scripts/release.json"),
        config,
        ReleaseRequest(None, False, True),
        git_environment,
    )
    tag = config.tag_prefix + expected
    prepared = git_command(repository, ("rev-parse", "HEAD"), git_environment).strip()
    assert (
        git_command(repository, ("rev-parse", f"{tag}^{{commit}}"), git_environment).strip()
        == prepared
    )
    remote = repository.parent / "remote.git"
    assert (
        git_command(remote, ("rev-parse", f"{tag}^{{commit}}"), git_environment).strip() == prepared
    )
    assert git_command(repository, ("status", "--porcelain"), git_environment) == ""
    parent = before.head
    for index, stage in enumerate(config.stages):
        for path in stage.files:
            target = repository / ("dist/example.txt" if path == "dist" else path)
            assert target.read_text() == f"{expected}:{parent}"
        remaining = len(config.stages) - index - 1
        parent = git_command(
            repository, ("rev-parse", f"HEAD~{remaining}"), git_environment
        ).strip()
    if not config.stages:
        assert prepared == before.head
