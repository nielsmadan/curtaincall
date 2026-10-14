from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type Command = tuple[str, ...]
type Version = tuple[int, ...]
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
    components: Literal[2, 3]
    initial_version: str
    tag_prefix: str
    paths: tuple[str, ...]
    tools: tuple[str, ...]
    checks: tuple[Command, ...]
    stages: tuple[Stage, ...]
    publication: str
    repository: str | None
    workflow: str | None
    suggest: Command | None
    draft: bool


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
    version: str | None
    counts: ChangeCounts
    source: Literal["initial", "conventional", "suggest", "override"]
    suggestion_skipped: bool


@dataclass(frozen=True, slots=True)
class TagState:
    name: str
    object_id: str
    commit_id: str


@dataclass(frozen=True, slots=True)
class ReleaseState:
    head: str
    branch: str
    origin: str
    remote_refs: RefSnapshot
    local_tags: tuple[TagState, ...]
    latest: str | None
    messages: tuple[str, ...]
    ahead: int


@dataclass(frozen=True, slots=True)
class ReleaseRequest:
    version: str | None
    dry_run: bool
    yes: bool
