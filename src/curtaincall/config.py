import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Literal, cast

from curtaincall.errors import ReleaseError
from curtaincall.models import Command, ReleaseConfig, Stage
from curtaincall.versions import parse_version


@dataclass(frozen=True)
class _Object:
    pairs: list[tuple[str, object]]


def _decode(value: object, context: str) -> object:
    if isinstance(value, _Object):
        result: dict[str, object] = {}
        for key, item in value.pairs:
            if key in result:
                raise ReleaseError(f"{context}.{key}: duplicate key")
            result[key] = _decode(item, f"{context}.{key}")
        return result
    if isinstance(value, list):
        return [_decode(item, f"{context}[{i}]") for i, item in enumerate(value)]
    return value


def _object(value: object, context: str, allowed: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReleaseError(f"{context}: expected an object")
    result = cast(dict[str, object], value)
    unknown = result.keys() - allowed
    if unknown:
        raise ReleaseError(f"{context}: unknown keys: {', '.join(sorted(unknown))}")
    return result


def _string(value: object, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value):
        raise ReleaseError(
            f"{context}: expected {'a' if empty else 'a nonempty'} string without NUL"
        )
    return value


def _array(value: object, context: str) -> list[object]:
    if not isinstance(value, list):
        raise ReleaseError(f"{context}: expected an array")
    return cast(list[object], value)


def _strings(value: object, context: str) -> tuple[str, ...]:
    return tuple(_string(item, f"{context}[{i}]") for i, item in enumerate(_array(value, context)))


def _path(value: str, context: str) -> str:
    parts = value.split("/")
    if (
        PurePosixPath(value).is_absolute()
        or any(p in {"", ".", ".."} for p in parts)
        or any(p.casefold() == ".git" for p in parts)
        or any(char in value for char in "*?[]:\\")
    ):
        raise ReleaseError(f"{context}: expected a literal repository-relative path")
    return value


def _paths(value: object, context: str) -> tuple[str, ...]:
    return tuple(_path(item, f"{context}[{i}]") for i, item in enumerate(_strings(value, context)))


def validate_template(value: str, context: str) -> None:
    try:
        fields = tuple(Formatter().parse(value))
    except ValueError as error:
        raise ReleaseError(f"{context}: malformed template: {error}") from error
    for _, field, spec, conversion in fields:
        if field is not None and (
            field not in {"version", "revision", "python"} or spec or conversion
        ):
            raise ReleaseError(
                f"{context}: only plain version, revision and python fields are allowed"
            )
    # Formatter discards an empty format spec, so reject that syntax before formatting.
    if re.search(r"\{(?:version|revision|python):", value.replace("{{", "").replace("}}", "")):
        raise ReleaseError(f"{context}: format specs are not allowed")


def _command(value: object, context: str, *, template: bool = False) -> Command:
    arguments = _array(value, context)
    if not arguments:
        raise ReleaseError(f"{context}: expected a nonempty argv array")
    result = tuple(
        _string(item, f"{context}[{i}]", empty=i > 0) for i, item in enumerate(arguments)
    )
    if template:
        for i, argument in enumerate(result):
            validate_template(argument, f"{context}[{i}]")
    return result


def _commands(value: object, context: str, *, template: bool = False) -> tuple[Command, ...]:
    return tuple(
        _command(item, f"{context}[{i}]", template=template)
        for i, item in enumerate(_array(value, context))
    )


def _stage(value: object, context: str) -> Stage:
    obj = _object(value, context, {"files", "commands", "checks", "message"})
    files = _paths(obj.get("files"), f"{context}.files")
    commands = _commands(obj.get("commands"), f"{context}.commands", template=True)
    if not files or not commands:
        raise ReleaseError(f"{context}: files and commands must be nonempty")
    checks = _commands(obj.get("checks", []), f"{context}.checks", template=True)
    message = _string(obj.get("message"), f"{context}.message")
    validate_template(message, f"{context}.message")
    return Stage(files, commands, checks, message)


def validate_ref(value: str, context: str, *, branch: bool = False) -> None:
    if (
        not value
        or value == "@"
        or value.endswith(("/", "."))
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(char <= " " or char == "\x7f" or char in "~^:?*[\\" for char in value)
        or any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
        or value.startswith("/")
        or (branch and (value == "HEAD" or value.startswith("-")))
    ):
        raise ReleaseError(f"{context}: invalid Git ref name {value!r}")


def _publication(obj: dict[str, object]) -> tuple[str | None, str | None, bool]:
    repository = (
        None if "repository" not in obj else _string(obj["repository"], "config.repository")
    )
    if repository is not None and not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseError("config.repository: expected owner/name")
    workflow = None if "workflow" not in obj else _string(obj["workflow"], "config.workflow")
    if workflow is not None and (
        repository is None or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow)
    ):
        raise ReleaseError("config.workflow: expected a workflow filename and a repository")
    draft = obj.get("draft", False)
    if not isinstance(draft, bool) or (draft and workflow is None):
        raise ReleaseError("config.draft: expected a boolean; true requires workflow")
    return repository, workflow, draft


def load_config(path: Path) -> ReleaseConfig:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_Object)
    except (OSError, UnicodeError, ValueError) as error:
        raise ReleaseError(f"Cannot read release config {path}: {error}") from error
    obj = _object(
        _decode(raw, "config"),
        "config",
        {
            "name",
            "branch",
            "components",
            "initial_version",
            "tag_prefix",
            "paths",
            "tools",
            "checks",
            "stages",
            "publication",
            "repository",
            "workflow",
            "suggest",
            "draft",
        },
    )
    components = obj.get("components", 3)
    if type(components) is not int or components not in (2, 3):
        raise ReleaseError("config.components: expected integer 2 or 3")
    component_count = cast(Literal[2, 3], components)
    initial = _string(obj.get("initial_version"), "config.initial_version")
    try:
        parse_version(initial, component_count)
    except ReleaseError as error:
        raise ReleaseError(f"config.initial_version: {error}") from error
    branch = _string(obj.get("branch", "main"), "config.branch")
    prefix = _string(obj.get("tag_prefix", "v"), "config.tag_prefix")
    validate_ref(branch, "config.branch", branch=True)
    validate_ref(prefix + initial, "config.tag_prefix")
    repository, workflow, draft = _publication(obj)
    return ReleaseConfig(
        name=_string(obj.get("name"), "config.name"),
        branch=branch,
        components=component_count,
        initial_version=initial,
        tag_prefix=prefix,
        paths=_paths(obj.get("paths", []), "config.paths"),
        tools=_strings(obj.get("tools", []), "config.tools"),
        checks=_commands(obj.get("checks", []), "config.checks"),
        stages=tuple(
            _stage(item, f"config.stages[{i}]")
            for i, item in enumerate(_array(obj.get("stages", []), "config.stages"))
        ),
        publication=_string(obj.get("publication", "Version tag pushed."), "config.publication"),
        repository=repository,
        workflow=workflow,
        suggest=None if "suggest" not in obj else _command(obj["suggest"], "config.suggest"),
        draft=draft,
    )


def validate_paths(root: Path, config: ReleaseConfig) -> None:
    resolved_root = root.resolve()
    paths = (*config.paths, *(path for stage in config.stages for path in stage.files))
    for path in paths:
        try:
            target = (root / path).resolve()
        except (OSError, RuntimeError) as error:
            raise ReleaseError(f"Cannot resolve configured path {path!r}: {error}") from error
        if (
            not target.is_relative_to(resolved_root)
            or ".git" in target.relative_to(resolved_root).parts
        ):
            raise ReleaseError(
                f"Configured path {path!r} resolves outside the worktree or into .git"
            )
