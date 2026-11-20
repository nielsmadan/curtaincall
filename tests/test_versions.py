from dataclasses import replace
from typing import Literal

import pytest

from curtaincall.errors import ReleaseError
from curtaincall.models import (
    ChangeCounts,
    ReleaseConfig,
    ReleaseState,
    RepositorySnapshot,
    Version,
)
from curtaincall.versions import (
    count_changes,
    format_version,
    make_proposal,
    parse_version,
    previous_tag,
    require_release_tag,
    select_version,
    tag_version,
)


def state(latest: str | None, *messages: str) -> ReleaseState:
    parsed = None if latest is None else tuple(map(int, latest.split(".")))
    return ReleaseState(RepositorySnapshot("head", "origin", (), ()), parsed, messages, 1)


def text(version: Version | None) -> str | None:
    return None if version is None else format_version(version)


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
    proposal = make_proposal(config, state(latest, message))
    assert text(proposal.version) == expected


def test_counts_overlap_for_breaking_features() -> None:
    assert count_changes(
        ("feat!: changed", "fix: repaired", "perf: fast", "chore: deps")
    ) == ChangeCounts(1, 2, 1)


@pytest.mark.parametrize(
    "message,expected", [("fix: repair", "1.3"), ("feat: add", "1.3"), ("fix!: break", "2.0")]
)
def test_two_part_proposals(config: ReleaseConfig, message: str, expected: str) -> None:
    policy = replace(config, version_parts=2, initial_version=(0, 1))
    assert text(make_proposal(policy, state("1.2", message)).version) == expected
    with pytest.raises(ReleaseError, match=r"MAJOR\.MINOR"):
        select_version(policy, "patch", (1, 2))


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
    assert text(select_version(config, selection, (1, 2, 3))) == expected
    assert text(make_proposal(config, state("1.2.3"), override=selection).version) == expected


@pytest.mark.parametrize("selection", ["patch", "minor", "major"])
def test_bump_keyword_on_first_release_selects_initial_version(
    config: ReleaseConfig, selection: str
) -> None:
    assert select_version(config, selection, None) == (0, 1, 0)
    two_part = replace(config, version_parts=2, initial_version=(1, 0))
    assert select_version(two_part, selection, None) == (1, 0)


def test_exact_first_release_version(config: ReleaseConfig) -> None:
    assert select_version(config, "v2.0.0", None) == (2, 0, 0)


@pytest.mark.parametrize("value", ["1.2.3", "1.2.2", "0.9.0"])
def test_override_must_increase(config: ReleaseConfig, value: str) -> None:
    with pytest.raises(ReleaseError, match="newer"):
        select_version(config, value, (1, 2, 3))


@pytest.mark.parametrize(
    "suggestion,expected", [("python-v1.3.0", "1.3.0"), ("1.2.3", None), ("1.0.0", None)]
)
def test_custom_suggestions(config: ReleaseConfig, suggestion: str, expected: str | None) -> None:
    policy = replace(config, suggest=("custom",), tag_prefix="python-v")
    result = make_proposal(policy, state("1.2.3", "feat: x"), suggested=suggestion)
    assert text(result.version) == expected


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
        version_parts=components,
        initial_version=(0, 1, 0) if components == 3 else (0, 1),
        tag_prefix=prefix,
        suggest=("custom",),
    )
    latest = "0.9.0" if components == 3 else "0.9"
    parsed = tuple(map(int, latest.split(".")))
    assert text(select_version(policy, value + suffix, parsed)) == expected + suffix
    result = make_proposal(policy, state(latest), suggested=value + suffix)
    assert text(result.version) == expected + suffix


@pytest.mark.parametrize(
    "suggestion, reason",
    [
        (None, "did not return a version"),
        ("", "canonical"),
        ("v1.3.0", "canonical"),
        ("1.3.0\nextra", "canonical"),
    ],
)
def test_invalid_custom_suggestion(
    config: ReleaseConfig, suggestion: str | None, reason: str
) -> None:
    policy = replace(config, suggest=("custom",), tag_prefix="python-v")
    with pytest.raises(ReleaseError, match=reason):
        make_proposal(policy, state("1.2.3"), suggested=suggestion)


def test_skipped_suggestion_estimate(config: ReleaseConfig) -> None:
    policy = replace(config, suggest=("custom",))
    result = make_proposal(policy, state("1.2.3", "fix: x"), suggestion_skipped=True)
    assert (text(result.version), result.suggestion_skipped) == ("1.2.4", True)
    first = make_proposal(policy, state(None), suggestion_skipped=True)
    assert (text(first.version), first.suggestion_skipped) == ("0.1.0", False)


@pytest.mark.parametrize("components", [2, 3])
def test_huge_version_has_release_error(components: Literal[2, 3]) -> None:
    with pytest.raises(ReleaseError, match="too large"):
        parse_version("9" * 5000 + ".0" * (components - 1), components)


def test_tag_version_selects_policy_namespace(config: ReleaseConfig) -> None:
    assert tag_version(config, "v1.10.0") == (1, 10, 0)
    assert tag_version(config, "rust-v1.0.0") is None
    assert tag_version(config, "v1.2") is None


@pytest.mark.parametrize(
    "tag", ["v1.2", "v01.2.3", "v1.2.3rc1", "v1.2.3\n", "--help", "rust-v1.0.0", "1.2.3"]
)
def test_require_release_tag_rejects_invalid_tags(config: ReleaseConfig, tag: str) -> None:
    with pytest.raises(ReleaseError, match="Invalid release tag"):
        require_release_tag(config, tag)


def test_require_release_tag_returns_version(config: ReleaseConfig) -> None:
    policy = replace(config, tag_prefix="python-v")
    assert require_release_tag(policy, "python-v2.3.4") == (2, 3, 4)


def test_previous_tag_ignores_other_namespaces(config: ReleaseConfig) -> None:
    tags = (
        "python-v1.9.0",
        "python-v1.10.0",
        "python-v2.0.0",
        "python-v1.10.0-rc.1",
        "rust-v1.11.0",
        "v1.12.0",
        "python-v01.11.0",
        "python-v1.11",
    )
    policy = replace(config, tag_prefix="python-v")
    assert previous_tag(policy, "python-v1.11.0", tags) == "python-v1.10.0"


def test_previous_tag_two_part_policy(config: ReleaseConfig) -> None:
    policy = replace(config, version_parts=2, initial_version=(0, 1))
    assert previous_tag(policy, "v1.10", ("v1.2", "v1.9", "v1.9.5", "v1.10", "v2.0")) == "v1.9"


def test_previous_tag_requires_strictly_lower_version(config: ReleaseConfig) -> None:
    assert previous_tag(config, "v1.0.0", ("v1.0.0", "v1.0.1", "v2.0.0")) is None
    assert previous_tag(config, "v0.1.0", ()) is None


def test_previous_tag_numeric_prefix(config: ReleaseConfig) -> None:
    policy = replace(config, tag_prefix="1")
    assert tag_version(policy, "11.2.0") == (1, 2, 0)
    assert tag_version(policy, "1.2.0") is None
    assert previous_tag(policy, "12.0.0", ("1.5.0", "10.9.0", "11.2.0", "13.0.0")) == "11.2.0"


def test_previous_tag_rejects_unparseable_tag(config: ReleaseConfig) -> None:
    with pytest.raises(ReleaseError, match=r"Invalid release tag: 'v1\.2'"):
        previous_tag(config, "v1.2", ("v1.0.0",))
