from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type Command = tuple[str, ...]
type Version = tuple[int, ...]
type VersionParts = Literal[2, 3]
type BumpKind = Literal["major", "minor", "patch"]
type RefSnapshot = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Stage:
    files: tuple[str, ...]
    commands: tuple[Command, ...]
    checks: tuple[Command, ...]
    message: str


@dataclass(frozen=True, slots=True)
class ReleaseConfig:
    name: str
    branch: str
    version_parts: VersionParts
    initial_version: Version
    tag_prefix: str
    history_paths: tuple[str, ...]
    tools: tuple[str, ...]
    preflight: tuple[Command, ...]
    stages: tuple[Stage, ...]
    success_message: str
    repository: str | None
    workflow: str | None
    suggest: Command | None
    draft: bool
    sign: bool


@dataclass(frozen=True, slots=True)
class Location:
    root: Path
    config_path: Path


@dataclass(frozen=True, slots=True)
class ChangeCounts:
    features: int
    fixes: int
    breaking: int


@dataclass(frozen=True, slots=True)
class Proposal:
    version: Version | None
    counts: ChangeCounts
    suggestion_skipped: bool


@dataclass(frozen=True, slots=True)
class TagState:
    name: str
    object_id: str
    commit_id: str
    version: Version


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    head: str
    origin: str
    remote_refs: RefSnapshot
    local_tags: tuple[TagState, ...]


@dataclass(frozen=True, slots=True)
class ReleaseState:
    snapshot: RepositorySnapshot
    latest: Version | None
    messages: tuple[str, ...]
    ahead: int


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    version: str | None
    dry_run: bool
    yes: bool


@dataclass(frozen=True, slots=True)
class PinAsset:
    name: str
    digest: str


@dataclass(frozen=True, slots=True)
class PinReference:
    file: str
    pattern: str


@dataclass(frozen=True, slots=True)
class Pin:
    name: str
    repository: str
    tag_prefix: str
    version: Version
    commit: str
    assets: tuple[PinAsset, ...]
    references: tuple[PinReference, ...]
    after_update: tuple[Command, ...]
