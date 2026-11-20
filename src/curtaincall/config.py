import json
import re
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Final, cast

from curtaincall.errors import ReleaseError
from curtaincall.models import Command, ReleaseConfig, Stage, Version, VersionParts
from curtaincall.versions import format_version, parse_version


@dataclass(frozen=True, slots=True)
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


def _object(value: object, context: str, allowed: frozenset[str] | None) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ReleaseError(f"{context}: expected an object")
    result = cast(dict[str, object], value)
    unknown = set() if allowed is None else result.keys() - allowed
    if unknown:
        raise ReleaseError(f"{context}: unknown keys: {', '.join(sorted(unknown))}")
    return result


def _string(value: object, context: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value):
        raise ReleaseError(
            f"{context}: expected {'a' if empty else 'a nonempty'} string without NUL"
        )
    return value


def _boolean(obj: dict[str, object], key: str) -> bool:
    value = obj.get(key, False)
    if not isinstance(value, bool):
        raise ReleaseError(f"policy.{key}: expected a boolean")
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


TEMPLATE_FIELDS: Final = ("version", "revision", "python", "tag", "tag_prefix")
_TEMPLATE_FIELD_LIST: Final = ", ".join(TEMPLATE_FIELDS[:-1]) + f" and {TEMPLATE_FIELDS[-1]}"


def template_values(
    config: ReleaseConfig, *, version: Version, revision: str, python: str
) -> dict[str, str]:
    text = format_version(version)
    return {
        "version": text,
        "revision": revision,
        "python": python,
        "tag": config.tag_prefix + text,
        "tag_prefix": config.tag_prefix,
    }


def validate_template(value: str, context: str) -> None:
    try:
        parsed = tuple(Formatter().parse(value))
    except ValueError as error:
        raise ReleaseError(f"{context}: malformed template: {error}") from error
    if any(
        field is not None and (field not in TEMPLATE_FIELDS or spec or conversion)
        for _, field, spec, conversion in parsed
    ):
        raise ReleaseError(f"{context}: only plain {_TEMPLATE_FIELD_LIST} fields are allowed")
    # Formatter discards an empty format spec, so reject that syntax before formatting.
    if re.search(
        r"\{(?:" + "|".join(sorted(TEMPLATE_FIELDS)) + r"):",
        value.replace("{{", "").replace("}}", ""),
    ):
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


_STAGE_KEYS: Final = frozenset(field.name for field in fields(Stage))
_POLICY_KEYS: Final = frozenset(field.name for field in fields(ReleaseConfig))


def _stage(value: object, context: str) -> Stage:
    obj = _object(value, context, _STAGE_KEYS)
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


@dataclass(frozen=True, slots=True)
class _TagPolicy:
    name: str
    branch: str
    version_parts: VersionParts
    initial_version: Version
    tag_prefix: str
    repository: str | None
    workflow: str | None
    draft: bool


def _tag_policy(obj: dict[str, object]) -> _TagPolicy:
    parts = obj.get("version_parts", 3)
    if type(parts) is not int or parts not in (2, 3):
        raise ReleaseError("policy.version_parts: expected integer 2 or 3")
    version_parts = cast(VersionParts, parts)
    initial = _string(obj.get("initial_version"), "policy.initial_version")
    try:
        initial_version = parse_version(initial, version_parts)
    except ReleaseError as error:
        raise ReleaseError(f"policy.initial_version: {error}") from error
    branch = _string(obj.get("branch", "main"), "policy.branch")
    prefix = _string(obj.get("tag_prefix", "v"), "policy.tag_prefix")
    validate_ref(branch, "policy.branch", branch=True)
    validate_ref(prefix + initial, "policy.tag_prefix")
    repository = (
        None if "repository" not in obj else _string(obj["repository"], "policy.repository")
    )
    if repository is not None and not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", repository):
        raise ReleaseError("policy.repository: expected owner/name")
    workflow = None if "workflow" not in obj else _string(obj["workflow"], "policy.workflow")
    if workflow is not None and (
        repository is None or not re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow)
    ):
        raise ReleaseError("policy.workflow: expected a workflow filename and a repository")
    draft = _boolean(obj, "draft")
    if draft and repository is None:
        raise ReleaseError("policy.draft: true requires repository")
    return _TagPolicy(
        name=_string(obj.get("name"), "policy.name"),
        branch=branch,
        version_parts=version_parts,
        initial_version=initial_version,
        tag_prefix=prefix,
        repository=repository,
        workflow=workflow,
        draft=draft,
    )


def _read_policy(path: Path, allowed: frozenset[str] | None) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_Object)
    except (OSError, UnicodeError, ValueError) as error:
        raise ReleaseError(f"Cannot read release policy {path}: {error}") from error
    return _object(_decode(raw, "policy"), "policy", allowed)


def load_config(path: Path) -> ReleaseConfig:
    obj = _read_policy(path, _POLICY_KEYS)
    tag = _tag_policy(obj)
    return ReleaseConfig(
        name=tag.name,
        branch=tag.branch,
        version_parts=tag.version_parts,
        initial_version=tag.initial_version,
        tag_prefix=tag.tag_prefix,
        history_paths=_paths(obj.get("history_paths", []), "policy.history_paths"),
        tools=_strings(obj.get("tools", []), "policy.tools"),
        preflight=_commands(obj.get("preflight", []), "policy.preflight"),
        stages=tuple(
            _stage(item, f"policy.stages[{i}]")
            for i, item in enumerate(_array(obj.get("stages", []), "policy.stages"))
        ),
        success_message=_string(
            obj.get("success_message", "Version tag pushed."), "policy.success_message"
        ),
        repository=tag.repository,
        workflow=tag.workflow,
        suggest=None if "suggest" not in obj else _command(obj["suggest"], "policy.suggest"),
        draft=tag.draft,
        sign=_boolean(obj, "sign"),
    )


def load_tag_policy(path: Path) -> ReleaseConfig:
    tag = _tag_policy(_read_policy(path, None))
    return ReleaseConfig(
        name=tag.name,
        branch=tag.branch,
        version_parts=tag.version_parts,
        initial_version=tag.initial_version,
        tag_prefix=tag.tag_prefix,
        history_paths=(),
        tools=(),
        preflight=(),
        stages=(),
        success_message="",
        repository=tag.repository,
        workflow=tag.workflow,
        suggest=None,
        draft=tag.draft,
        sign=False,
    )


def validate_paths(root: Path, config: ReleaseConfig) -> None:
    resolved_root = root.resolve()
    paths = (*config.history_paths, *(path for stage in config.stages for path in stage.files))
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
