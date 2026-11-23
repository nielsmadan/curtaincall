import json
import re
from dataclasses import dataclass, fields
from pathlib import Path, PurePosixPath
from string import Formatter
from typing import Final, cast

from curtaincall.errors import ReleaseError
from curtaincall.models import (
    Command,
    Pin,
    PinAsset,
    PinReference,
    ReleaseConfig,
    Stage,
    Version,
    VersionParts,
)
from curtaincall.versions import format_version, parse_any_version, parse_version


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


REPOSITORY: Final = re.compile(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+")
ASSET_NAME: Final = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9_+-]|\.(?=[A-Za-z0-9_+-]))*")
PIN_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]*")
DIGEST: Final = re.compile(r"sha256:[0-9a-f]{64}")
COMMIT: Final = re.compile(r"[0-9a-f]{40}")
TEMPLATE_FIELDS: Final = ("version", "revision", "python", "tag", "tag_prefix")
PIN_TEMPLATE_FIELDS: Final = ("version",)
PIN_COMMAND_FIELDS: Final = ("version", "tag")


def _field_list(allowed: tuple[str, ...]) -> str:
    return allowed[0] if len(allowed) == 1 else ", ".join(allowed[:-1]) + f" and {allowed[-1]}"


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


def literal_template(value: str) -> str:
    return value.replace("{", "{{").replace("}", "}}")


def validate_template(value: str, context: str, allowed: tuple[str, ...] = TEMPLATE_FIELDS) -> None:
    try:
        parsed = tuple(Formatter().parse(value))
    except ValueError as error:
        raise ReleaseError(f"{context}: malformed template: {error}") from error
    if any(
        field is not None and (field not in allowed or spec or conversion)
        for _, field, spec, conversion in parsed
    ):
        raise ReleaseError(f"{context}: only plain {_field_list(allowed)} fields are allowed")
    # Formatter discards an empty format spec, so reject that syntax before formatting.
    if re.search(
        r"\{(?:" + "|".join(sorted(allowed)) + r"):",
        value.replace("{{", "").replace("}}", ""),
    ):
        raise ReleaseError(f"{context}: format specs are not allowed")


def _command(value: object, context: str, *, template: tuple[str, ...] = ()) -> Command:
    arguments = _array(value, context)
    if not arguments:
        raise ReleaseError(f"{context}: expected a nonempty argv array")
    result = tuple(
        _string(item, f"{context}[{i}]", empty=i > 0) for i, item in enumerate(arguments)
    )
    if template:
        for i, argument in enumerate(result):
            validate_template(argument, f"{context}[{i}]", template)
    return result


def _commands(
    value: object, context: str, *, template: tuple[str, ...] = ()
) -> tuple[Command, ...]:
    return tuple(
        _command(item, f"{context}[{i}]", template=template)
        for i, item in enumerate(_array(value, context))
    )


_STAGE_KEYS: Final = frozenset(field.name for field in fields(Stage))
_POLICY_KEYS: Final = frozenset(field.name for field in fields(ReleaseConfig))


def _stage(value: object, context: str) -> Stage:
    obj = _object(value, context, _STAGE_KEYS)
    files = _paths(obj.get("files"), f"{context}.files")
    commands = _commands(obj.get("commands"), f"{context}.commands", template=TEMPLATE_FIELDS)
    if not files or not commands:
        raise ReleaseError(f"{context}: files and commands must be nonempty")
    checks = _commands(obj.get("checks", []), f"{context}.checks", template=TEMPLATE_FIELDS)
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
    if repository is not None and not REPOSITORY.fullmatch(repository):
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


def _pin_name(name: str, context: str) -> str:
    if not PIN_NAME.fullmatch(name):
        raise ReleaseError(f"{context}: pin names use lowercase letters, digits, '-' and '_'")
    return name


def _pin_names(value: object, context: str) -> tuple[str, ...]:
    names = tuple(
        _pin_name(name, f"{context}[{i}]") for i, name in enumerate(_strings(value, context))
    )
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ReleaseError(f"{context}: duplicate pin names: {', '.join(duplicates)}")
    return names


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
        pins=_pin_names(obj.get("pins", []), "policy.pins"),
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
        pins=(),
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


_PIN_KEYS: Final = frozenset(
    {
        "repository",
        "tag_prefix",
        "version",
        "commit",
        "assets",
        "references",
        "files",
        "after_update",
    }
)
_REFERENCE_KEYS: Final = frozenset(field.name for field in fields(PinReference))


def asset_name(template: str, version: Version) -> str:
    return template.format(version=format_version(version))


def validate_asset_template(name: str, version: Version, context: str) -> None:
    validate_template(name, context, PIN_TEMPLATE_FIELDS)
    if not ASSET_NAME.fullmatch(asset_name(name, version)):
        raise ReleaseError(f"{context}: invalid release asset name")


def _pin_asset(name: str, digest: object, version: Version, context: str) -> PinAsset:
    validate_asset_template(name, version, context)
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise ReleaseError(f"{context}: expected sha256:<64 lowercase hex digits>")
    return PinAsset(name, digest)


def _pin_reference(value: object, context: str) -> PinReference:
    obj = _object(value, context, _REFERENCE_KEYS)
    file = _path(_string(obj.get("file"), f"{context}.file"), f"{context}.file")
    pattern = _string(obj.get("pattern"), f"{context}.pattern")
    validate_template(pattern, f"{context}.pattern", PIN_TEMPLATE_FIELDS)
    if "{version}" not in pattern.replace("{{", "").replace("}}", ""):
        raise ReleaseError(f"{context}.pattern: must contain {{version}}")
    return PinReference(file, pattern)


def _pin(name: str, value: object) -> Pin:
    context = f"pins.{name}"
    _pin_name(name, context)
    obj = _object(value, context, _PIN_KEYS)
    repository = _string(obj.get("repository"), f"{context}.repository")
    if not REPOSITORY.fullmatch(repository):
        raise ReleaseError(f"{context}.repository: expected owner/name")
    prefix = _string(obj.get("tag_prefix", "v"), f"{context}.tag_prefix", empty=True)
    try:
        version = parse_any_version(_string(obj.get("version"), f"{context}.version"))
    except ReleaseError as error:
        raise ReleaseError(f"{context}.version: {error}") from error
    validate_ref(prefix + format_version(version), f"{context}.tag_prefix")
    commit = _string(obj.get("commit"), f"{context}.commit")
    if not COMMIT.fullmatch(commit):
        raise ReleaseError(f"{context}.commit: expected a full lowercase commit SHA")
    assets = _object(obj.get("assets"), f"{context}.assets", None)
    if not assets:
        raise ReleaseError(f"{context}.assets: expected at least one asset")
    return Pin(
        name=name,
        repository=repository,
        tag_prefix=prefix,
        version=version,
        commit=commit,
        assets=tuple(
            _pin_asset(asset, digest, version, f"{context}.assets.{asset}")
            for asset, digest in assets.items()
        ),
        references=tuple(
            _pin_reference(item, f"{context}.references[{i}]")
            for i, item in enumerate(_array(obj.get("references", []), f"{context}.references"))
        ),
        files=_paths(obj.get("files", []), f"{context}.files"),
        after_update=_commands(
            obj.get("after_update", []), f"{context}.after_update", template=PIN_COMMAND_FIELDS
        ),
    )


def parse_pins(text: str, source: str) -> tuple[Pin, ...]:
    try:
        raw: object = json.loads(text, object_pairs_hook=_Object)
    except ValueError as error:
        raise ReleaseError(f"Cannot read pins {source}: {error}") from error
    pins = _object(_decode(raw, "pins"), "pins", None)
    return tuple(_pin(name, value) for name, value in pins.items())


def load_pins(path: Path) -> tuple[Pin, ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ()
    except (OSError, UnicodeError) as error:
        raise ReleaseError(f"Cannot read pins {path}: {error}") from error
    return parse_pins(text, str(path))


def _pin_json(pin: Pin) -> dict[str, object]:
    return {
        "repository": pin.repository,
        **({} if pin.tag_prefix == "v" else {"tag_prefix": pin.tag_prefix}),
        "version": format_version(pin.version),
        "commit": pin.commit,
        "assets": {asset.name: asset.digest for asset in pin.assets},
        **(
            {"references": [{"file": r.file, "pattern": r.pattern} for r in pin.references]}
            if pin.references
            else {}
        ),
        **({"files": list(pin.files)} if pin.files else {}),
        **(
            {"after_update": [list(command) for command in pin.after_update]}
            if pin.after_update
            else {}
        ),
    }


def dump_pins(pins: tuple[Pin, ...]) -> str:
    return json.dumps({pin.name: _pin_json(pin) for pin in pins}, indent=2) + "\n"
