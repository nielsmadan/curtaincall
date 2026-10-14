import re
from typing import Final, Literal

from curtaincall.errors import ReleaseError
from curtaincall.models import ChangeCounts, Proposal, ReleaseConfig, ReleaseState, Version

TWO_PART_VERSION: Final = 2
SEMVER_PARTS: Final = 3


def _version_pattern(components: Literal[2, 3]) -> str:
    return r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){" + str(components - 1) + "}"


def parse_version(value: str, components: Literal[2, 3]) -> Version:
    if not re.fullmatch(_version_pattern(components), value):
        raise ReleaseError(f"Expected a canonical {components}-part version, got {value!r}.")
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as error:
        raise ReleaseError(f"Version component is too large: {value!r}.") from error


def _normalize_version(value: str, config: ReleaseConfig) -> str:
    return (
        value
        if re.fullmatch(_version_pattern(config.components), value)
        else value.removeprefix(config.tag_prefix)
    )


def bump_version(base: Version, kind: str) -> str:
    if kind == "patch" and len(base) == TWO_PART_VERSION:
        raise ReleaseError("This project uses MAJOR.MINOR; choose minor or an exact version.")
    index = {"major": 0, "minor": 1, "patch": 2}[kind]
    return ".".join(
        str(part if i < index else part + 1 if i == index else 0) for i, part in enumerate(base)
    )


def select_version(value: str, latest: str | None, config: ReleaseConfig) -> str:
    base = parse_version(latest or config.initial_version, config.components)
    candidate = (
        bump_version(base, value)
        if value in {"patch", "minor", "major"}
        else _normalize_version(value, config)
    )
    parsed = parse_version(candidate, config.components)
    if latest is not None and parsed <= base:
        raise ReleaseError(f"The release must be newer than {latest}.")
    return candidate


def count_changes(messages: tuple[str, ...]) -> ChangeCounts:
    features = fixes = breaking = 0
    for message in messages:
        header = message.partition("\n")[0]
        match = re.fullmatch(r"(\w+)(?:\([^\n]*\))?(!)?:.*", header)
        if match is None:
            continue
        features += match[1] == "feat"
        fixes += match[1] in {"fix", "perf"}
        breaking += bool(match[2] or re.search(r"^BREAKING[ -]CHANGE:", message, re.MULTILINE))
    return ChangeCounts(features, fixes, breaking)


def conventional_version(base: Version, counts: ChangeCounts) -> str | None:
    if counts.breaking:
        kind = "minor" if len(base) == SEMVER_PARTS and base[0] == 0 else "major"
    elif counts.features or (len(base) == TWO_PART_VERSION and counts.fixes):
        kind = "minor"
    elif counts.fixes:
        kind = "patch"
    else:
        return None
    return bump_version(base, kind)


def make_proposal(
    config: ReleaseConfig,
    state: ReleaseState,
    override: str | None,
    suggested_version: str | None,
    suggestion_skipped: bool,
) -> Proposal:
    counts = count_changes(state.messages)
    if override is not None:
        return Proposal(select_version(override, state.latest, config), counts, "override", False)
    if state.latest is None:
        return Proposal(config.initial_version, counts, "initial", False)
    base = parse_version(state.latest, config.components)
    if config.suggest is not None and not suggestion_skipped:
        if suggested_version is None:
            raise ReleaseError("The suggestion command did not return a version.")
        candidate = _normalize_version(suggested_version, config)
        parsed = parse_version(candidate, config.components)
        return Proposal(candidate if parsed > base else None, counts, "suggest", False)
    return Proposal(conventional_version(base, counts), counts, "conventional", suggestion_skipped)
