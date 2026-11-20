import re
from collections.abc import Iterable
from typing import Final, cast, get_args

from curtaincall.errors import ReleaseError
from curtaincall.models import (
    BumpKind,
    ChangeCounts,
    Proposal,
    ReleaseConfig,
    ReleaseState,
    Version,
    VersionParts,
)

_VERSION_PATTERNS: Final = {
    parts: re.compile(r"(0|[1-9][0-9]*)(\.(0|[1-9][0-9]*)){" + str(parts - 1) + "}")
    for parts in (2, 3)
}
_HEADER: Final = re.compile(r"(\w+)(?:\([^\n]*\))?(!)?:.*")
_BREAKING_FOOTER: Final = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
BUMP_KINDS: Final = frozenset(get_args(BumpKind.__value__))
_MAJOR_MINOR: Final = 2
_SEMVER: Final = 3


def format_version(version: Version) -> str:
    return ".".join(map(str, version))


def parse_version(value: str, parts: VersionParts) -> Version:
    if not _VERSION_PATTERNS[parts].fullmatch(value):
        raise ReleaseError(f"Expected a canonical {parts}-part version, got {value!r}.")
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError as error:
        raise ReleaseError(f"Version component is too large: {value!r}.") from error


def release_tag(config: ReleaseConfig, version: Version) -> str:
    return config.tag_prefix + format_version(version)


def tag_version(config: ReleaseConfig, name: str) -> Version | None:
    if not name.startswith(config.tag_prefix):
        return None
    try:
        return parse_version(name[len(config.tag_prefix) :], config.version_parts)
    except ReleaseError:
        return None


def require_release_tag(config: ReleaseConfig, tag: str) -> Version:
    parsed = tag_version(config, tag)
    if parsed is None:
        raise ReleaseError(f"Invalid release tag: {tag!r}")
    return parsed


def version_text(config: ReleaseConfig, tag: str) -> str:
    return format_version(require_release_tag(config, tag))


def previous_tag(config: ReleaseConfig, tag: str, tags: Iterable[str]) -> str | None:
    current = require_release_tag(config, tag)
    candidates = [
        (parsed, name)
        for name in tags
        if (parsed := tag_version(config, name)) is not None and parsed < current
    ]
    return max(candidates)[1] if candidates else None


def _normalize_version(config: ReleaseConfig, value: str) -> str:
    return (
        value
        if _VERSION_PATTERNS[config.version_parts].fullmatch(value)
        else value.removeprefix(config.tag_prefix)
    )


def bump_version(base: Version, kind: BumpKind) -> Version:
    if kind == "patch" and len(base) == _MAJOR_MINOR:
        raise ReleaseError("This project uses MAJOR.MINOR; choose minor or an exact version.")
    index = ("major", "minor", "patch").index(kind)
    return tuple(
        part if i < index else part + 1 if i == index else 0 for i, part in enumerate(base)
    )


def select_version(config: ReleaseConfig, value: str, latest: Version | None) -> Version:
    if value in BUMP_KINDS:
        if latest is None:
            return config.initial_version
        return bump_version(latest, cast(BumpKind, value))
    candidate = parse_version(_normalize_version(config, value), config.version_parts)
    if latest is not None and candidate <= latest:
        raise ReleaseError(f"The release must be newer than {format_version(latest)}.")
    return candidate


def classify(message: str) -> ChangeCounts:
    match = _HEADER.fullmatch(message.partition("\n")[0])
    if match is None:
        return ChangeCounts(0, 0, 0)
    return ChangeCounts(
        features=int(match[1] == "feat"),
        fixes=int(match[1] in {"fix", "perf"}),
        breaking=int(bool(match[2] or _BREAKING_FOOTER.search(message))),
    )


def count_changes(messages: Iterable[str]) -> ChangeCounts:
    counts = [classify(message) for message in messages]
    return ChangeCounts(
        features=sum(count.features for count in counts),
        fixes=sum(count.fixes for count in counts),
        breaking=sum(count.breaking for count in counts),
    )


def conventional_version(base: Version, counts: ChangeCounts) -> Version | None:
    kind: BumpKind
    if counts.breaking:
        kind = "minor" if len(base) == _SEMVER and base[0] == 0 else "major"
    elif counts.features or (len(base) == _MAJOR_MINOR and counts.fixes):
        kind = "minor"
    elif counts.fixes:
        kind = "patch"
    else:
        return None
    return bump_version(base, kind)


def make_proposal(
    config: ReleaseConfig,
    state: ReleaseState,
    *,
    override: str | None = None,
    suggested: str | None = None,
    suggestion_skipped: bool = False,
) -> Proposal:
    counts = count_changes(state.messages)
    if override is not None:
        return Proposal(select_version(config, override, state.latest), counts, False)
    if state.latest is None:
        return Proposal(config.initial_version, counts, False)
    if config.suggest is not None and not suggestion_skipped:
        if suggested is None:
            raise ReleaseError("The suggestion command did not return a version.")
        candidate = parse_version(_normalize_version(config, suggested), config.version_parts)
        return Proposal(candidate if candidate > state.latest else None, counts, False)
    return Proposal(conventional_version(state.latest, counts), counts, suggestion_skipped)
