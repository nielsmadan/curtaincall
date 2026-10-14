from dataclasses import replace
from importlib.metadata import version
from typing import Literal

import pytest

from curtaincall.cli import main
from curtaincall.errors import ReleaseError
from curtaincall.models import ChangeCounts, ReleaseConfig, ReleaseState
from curtaincall.versions import count_changes, make_proposal, parse_version, select_version


def state(latest: str | None, *messages: str) -> ReleaseState:
    return ReleaseState("head", "main", "origin", (), (), latest, messages, 1)


@pytest.mark.parametrize(
    "value",
    [
        "01.2.3",
        "1.2",
        "1.2.3.4",
        "v1.2.3",
        "1.2.3-rc1",
        "1.2.3\n",
        "-1.2.3",
        "\uff11.2.3",
        "1.2.+3",
    ],
)
def test_noncanonical_version(value: str) -> None:
    with pytest.raises(ReleaseError, match="canonical"):
        parse_version(value, 3)


def test_numeric_ordering() -> None:
    assert parse_version("1.10.0", 3) > parse_version("1.9.9", 3)
    assert parse_version("0.1", 2) == (0, 1)


@pytest.mark.parametrize(
    "latest,message,expected",
    [
        (None, "chore: setup", "0.1.0"),
        ("1.2.3", "feat: add", "1.3.0"),
        ("1.2.3", "fix(scope): correct", "1.2.4"),
        ("1.2.3", "perf: faster", "1.2.4"),
        ("1.2.3", "refactor!: replace", "2.0.0"),
        ("1.2.3", "feat: add\n\nBREAKING CHANGE: replacement", "2.0.0"),
        ("1.2.3", "chore: deps\n\nBREAKING-CHANGE: remove", "2.0.0"),
        ("0.2.3", "fix!: break", "0.3.0"),
        ("1.2.3", "chore: cleanup", None),
        ("1.2.3", "", None),
        ("1.2.3", "ordinary message", None),
    ],
)
def test_proposals(
    config: ReleaseConfig, latest: str | None, message: str, expected: str | None
) -> None:
    proposal = make_proposal(config, state(latest, message), None, None, False)
    assert proposal.version == expected
    assert proposal.source == ("initial" if latest is None else "conventional")


def test_counts_overlap_for_breaking_features() -> None:
    assert count_changes(
        ("feat!: changed", "fix: repaired", "perf: fast", "chore: deps")
    ) == ChangeCounts(1, 2, 1)


@pytest.mark.parametrize(
    "message,expected", [("fix: repair", "1.3"), ("feat: add", "1.3"), ("fix!: break", "2.0")]
)
def test_two_part_proposals(config: ReleaseConfig, message: str, expected: str) -> None:
    policy = replace(config, components=2, initial_version="0.1")
    assert make_proposal(policy, state("1.2", message), None, None, False).version == expected
    with pytest.raises(ReleaseError, match=r"MAJOR\.MINOR"):
        select_version("patch", "1.2", policy)


@pytest.mark.parametrize(
    "selection,expected",
    [
        ("patch", "1.2.4"),
        ("minor", "1.3.0"),
        ("major", "2.0.0"),
        ("v1.10.0", "1.10.0"),
        ("2.0.0", "2.0.0"),
    ],
)
def test_explicit_selection(config: ReleaseConfig, selection: str, expected: str) -> None:
    assert select_version(selection, "1.2.3", config) == expected
    assert make_proposal(config, state("1.2.3"), selection, None, False).source == "override"


@pytest.mark.parametrize("value", ["1.2.3", "1.2.2", "0.9.0"])
def test_override_must_increase(config: ReleaseConfig, value: str) -> None:
    with pytest.raises(ReleaseError, match="newer"):
        select_version(value, "1.2.3", config)


@pytest.mark.parametrize(
    "suggestion,expected", [("python-v1.3.0", "1.3.0"), ("1.2.3", None), ("1.0.0", None)]
)
def test_custom_suggestions(config: ReleaseConfig, suggestion: str, expected: str | None) -> None:
    policy = replace(config, suggest=("custom",), tag_prefix="python-v")
    result = make_proposal(policy, state("1.2.3", "feat: x"), None, suggestion, False)
    assert (result.version, result.source) == (expected, "suggest")


@pytest.mark.parametrize("components", [2, 3])
@pytest.mark.parametrize(
    "prefix,value,expected",
    [("1", "1.2", "1.2"), ("1", "11.2", "11.2"), ("release-", "release-1.2", "1.2")],
)
def test_version_boundary_preserves_bare_versions(
    config: ReleaseConfig, components: Literal[2, 3], prefix: str, value: str, expected: str
) -> None:
    suffix = ".3" if components == 3 else ""
    policy = replace(
        config,
        components=components,
        initial_version="0.1.0" if components == 3 else "0.1",
        tag_prefix=prefix,
        suggest=("custom",),
    )
    latest = "0.9.0" if components == 3 else "0.9"
    assert select_version(value + suffix, latest, policy) == expected + suffix
    result = make_proposal(policy, state(latest), None, value + suffix, False)
    assert (result.version, result.source) == (expected + suffix, "suggest")


@pytest.mark.parametrize("suggestion", [None, "", "v1.3.0", "1.3.0\nextra"])
def test_invalid_custom_suggestion(config: ReleaseConfig, suggestion: str | None) -> None:
    policy = replace(config, suggest=("custom",), tag_prefix="python-v")
    with pytest.raises(ReleaseError):
        make_proposal(policy, state("1.2.3"), None, suggestion, False)


def test_skipped_suggestion_estimate(config: ReleaseConfig) -> None:
    policy = replace(config, suggest=("custom",))
    result = make_proposal(policy, state("1.2.3", "fix: x"), None, None, True)
    assert (result.version, result.source, result.suggestion_skipped) == (
        "1.2.4",
        "conventional",
        True,
    )
    assert make_proposal(policy, state(None), None, None, True).source == "initial"


@pytest.mark.parametrize("argument", ["--help", "--version"])
def test_cli_information(argument: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as error:
        main([argument])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert (version("curtaincall") if argument == "--version" else "--dry-run") in output


def test_execution_error_is_reported(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def unavailable(*_: object) -> None:
        raise ReleaseError("Repository unavailable.")

    monkeypatch.setattr("curtaincall.cli.resolve_location", unavailable)
    assert main([]) == 1
    assert capsys.readouterr().err == "curtains: Repository unavailable.\n"


@pytest.mark.parametrize("components", [2, 3])
def test_huge_version_has_release_error(components: Literal[2, 3]) -> None:
    with pytest.raises(ReleaseError, match="too large"):
        parse_version("9" * 5000 + ".0" * (components - 1), components)
