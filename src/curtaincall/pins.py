import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from urllib.parse import quote

from curtaincall.arguments import add_location_arguments
from curtaincall.config import (
    COMMIT,
    DIGEST,
    PIN_NAME,
    REPOSITORY,
    asset_name,
    dump_pins,
    load_pins,
    parse_pins,
    validate_asset_template,
    validate_ref,
)
from curtaincall.errors import ReleaseError, name_list
from curtaincall.git import resolve_location
from curtaincall.models import Command, Pin, PinAsset, PinReference, Version
from curtaincall.process import (
    ProcessResult,
    command_environment,
    process_failure,
    run_command,
    run_process,
)
from curtaincall.versions import format_version, parse_any_version

DEFAULT_FILE: Final = "scripts/pins.json"
_CHUNK_SIZE: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class RemoteRelease:
    tag: str
    digests: Mapping[str, str | None]


@dataclass(frozen=True, slots=True)
class CheckResult:
    pin: Pin
    latest: Version | None
    error: str | None

    @property
    def outdated(self) -> bool:
        return self.latest is not None and self.latest > self.pin.version


def pin_tag(tag_prefix: str, version: Version) -> str:
    return tag_prefix + format_version(version)


def release_version(repository: str, tag_prefix: str, tag: str) -> Version:
    if not tag.startswith(tag_prefix):
        raise ReleaseError(f"{repository} tag {tag!r} does not start with {tag_prefix!r}.")
    try:
        return parse_any_version(tag.removeprefix(tag_prefix))
    except ReleaseError as error:
        raise ReleaseError(f"{repository} tag {tag!r} is not a numeric release.") from error


def requested_tag(tag_prefix: str, requested: str) -> str:
    text = requested.removeprefix(tag_prefix) if tag_prefix else requested
    return pin_tag(tag_prefix, parse_any_version(text))


def parse_release(stdout: str, repository: str) -> RemoteRelease:
    invalid = f"GitHub returned invalid release metadata for {repository}."
    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise ReleaseError(invalid) from error
    if not isinstance(raw, dict) or not isinstance(raw.get("tagName"), str):
        raise ReleaseError(invalid)
    if raw.get("isDraft") is not False:
        raise ReleaseError(f"{repository} {raw['tagName']} is not a published release.")
    assets = raw.get("assets")
    if not isinstance(assets, list) or not all(
        isinstance(item, dict) and isinstance(item.get("name"), str) for item in assets
    ):
        raise ReleaseError(invalid)
    return RemoteRelease(
        raw["tagName"],
        {
            item["name"]: item["digest"]
            if isinstance(item.get("digest"), str) and DIGEST.fullmatch(item["digest"])
            else None
            for item in assets
        },
    )


def check_line(result: CheckResult) -> str:
    pinned = format_version(result.pin.version)
    if result.latest is None:
        return (
            f"{result.pin.name} {pinned}: could not check {result.pin.repository}: {result.error}"
        )
    latest = format_version(result.latest)
    if result.outdated:
        return (
            f"{result.pin.name} {pinned}: {latest} is available; "
            f"run: curtains pins update {result.pin.name}"
        )
    return f"{result.pin.name} {pinned}: current (latest release {latest})"


def check_json(results: Sequence[CheckResult]) -> str:
    return (
        json.dumps(
            [
                {
                    "name": result.pin.name,
                    "repository": result.pin.repository,
                    "pinned": format_version(result.pin.version),
                    "latest": None if result.latest is None else format_version(result.latest),
                    "outdated": result.outdated,
                    "error": result.error,
                }
                for result in results
            ],
            indent=2,
        )
        + "\n"
    )


def rewrite_references(
    texts: Mapping[str, str], references: Sequence[PinReference], old: Version, new: Version
) -> dict[str, str]:
    result = dict(texts)
    for reference in references:
        before = reference.pattern.format(version=format_version(old))
        after = reference.pattern.format(version=format_version(new))
        if before not in result[reference.file]:
            raise ReleaseError(f"{reference.file} does not contain {before!r}.")
        result[reference.file] = result[reference.file].replace(before, after)
    return result


def after_update_commands(pin: Pin) -> tuple[Command, ...]:
    values = {"version": format_version(pin.version), "tag": pin_tag(pin.tag_prefix, pin.version)}
    return tuple(
        tuple(argument.format(**values) for argument in command) for command in pin.after_update
    )


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
    except OSError as error:
        raise ReleaseError(f"Cannot read {path}: {error}") from error
    return f"sha256:{digest.hexdigest()}"


@dataclass(frozen=True, slots=True)
class _GitHub:
    root: Path
    environment: Mapping[str, str]

    def _gh(self, *arguments: str) -> ProcessResult:
        return run_process(self.root, ("gh", *arguments), self.environment, input_data=b"")

    def release(self, repository: str, tag: str | None) -> RemoteRelease:
        result = self._gh(
            "release",
            "view",
            "--repo",
            repository,
            "--json",
            "tagName,isDraft,assets",
            "--",
            *(() if tag is None else (tag,)),
        )
        if result.code == 1 and "release not found" in result.stderr:
            wanted = "published release" if tag is None else f"release {tag}"
            raise ReleaseError(f"{repository} has no {wanted}.")
        if result.code:
            raise ReleaseError(process_failure(f"gh release view --repo {repository}", result))
        return parse_release(result.stdout, repository)

    def commit(self, repository: str, tag: str) -> str:
        result = self._gh(
            "api",
            "--method",
            "GET",
            f"repos/{repository}/commits/{quote(tag, safe='')}",
            "--jq",
            ".sha",
        )
        if result.code:
            raise ReleaseError(process_failure(f"gh api {repository} commit of {tag}", result))
        commit = result.stdout.strip()
        if not COMMIT.fullmatch(commit):
            raise ReleaseError(f"GitHub returned no commit for {repository} {tag}.")
        return commit

    def download(self, repository: str, tag: str, name: str, directory: Path) -> Path:
        result = self._gh(
            "release",
            "download",
            "--repo",
            repository,
            "--pattern",
            name,
            "--dir",
            str(directory),
            "--",
            tag,
        )
        if result.code:
            raise ReleaseError(process_failure(f"gh release download {name}", result))
        path = directory / name
        if not path.is_file():
            raise ReleaseError(f"gh release download did not produce {name}.")
        return path


def _write_text(path: Path, content: str) -> None:
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as stream:
            stream.write(content)
        os.chmod(stream.name, mode)
        os.replace(stream.name, path)
    except OSError as error:
        raise ReleaseError(f"Cannot write {path}: {error}") from error


def _reference_path(root: Path, file: str) -> Path:
    path = root / file
    if not path.resolve().is_relative_to(root.resolve()):
        raise ReleaseError(f"Reference {file!r} resolves outside the worktree.")
    return path


def _release_assets(
    github: _GitHub,
    repository: str,
    templates: Sequence[str],
    release: RemoteRelease,
    version: Version,
) -> tuple[PinAsset, ...]:
    names = {template: asset_name(template, version) for template in templates}
    missing = [name for name in names.values() if name not in release.digests]
    if missing:
        raise ReleaseError(f"{repository} {release.tag} has no asset {name_list(missing)}.")
    with tempfile.TemporaryDirectory(prefix="curtains-pin-") as scratch:
        return tuple(
            PinAsset(
                template,
                release.digests[name]
                or file_digest(github.download(repository, release.tag, name, Path(scratch))),
            )
            for template, name in names.items()
        )


@dataclass(frozen=True, slots=True)
class _Context:
    root: Path
    path: Path
    pins: tuple[Pin, ...]
    github: _GitHub
    environment: Mapping[str, str]

    def find(self, name: str) -> Pin:
        found = next((pin for pin in self.pins if pin.name == name), None)
        if found is None:
            known = name_list(pin.name for pin in self.pins)
            raise ReleaseError(f"No pin named {name!r} in {self.path} (pins: {known}).")
        return found

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.root))

    def render(self, pins: tuple[Pin, ...]) -> str:
        content = dump_pins(pins)
        parse_pins(content, "the updated pins")
        return content


def _check_one(github: _GitHub, pin: Pin) -> CheckResult:
    try:
        release = github.release(pin.repository, None)
        return CheckResult(pin, release_version(pin.repository, pin.tag_prefix, release.tag), None)
    except ReleaseError as error:
        return CheckResult(pin, None, str(error))


def _check(context: _Context, args: argparse.Namespace) -> int:
    selected = [context.find(name) for name in args.names] if args.names else context.pins
    if not selected:
        raise ReleaseError(f"No pins in {context.path}; add one with 'curtains pins add'.")
    results = [_check_one(context.github, pin) for pin in selected]
    if args.json:
        sys.stdout.write(check_json(results))
    else:
        for result in results:
            print(check_line(result), file=sys.stderr if result.error else sys.stdout)
    failed = any(result.outdated or result.error for result in results)
    return 1 if args.strict and failed else 0


def _run_after_update(context: _Context, pin: Pin) -> None:
    for command in after_update_commands(pin):
        print(f"Running {' '.join(command)}", flush=True)
        try:
            run_command(context.root, command, context.environment, capture=False)
        except ReleaseError as error:
            raise ReleaseError(
                f"Pinned {pin.name} {format_version(pin.version)}, but a follow-up command "
                f"failed: {error}. The updated files are left for review."
            ) from error


def _update(context: _Context, args: argparse.Namespace) -> int:
    pin = context.find(args.name)
    tag = None if args.version is None else requested_tag(pin.tag_prefix, args.version)
    release = context.github.release(pin.repository, tag)
    version = release_version(pin.repository, pin.tag_prefix, release.tag)
    if tag is None and version <= pin.version:
        print(f"{pin.name} is current at {format_version(pin.version)}.")
        return 0
    assets = _release_assets(
        context.github, pin.repository, [asset.name for asset in pin.assets], release, version
    )
    if version == pin.version:
        if assets != pin.assets:
            raise ReleaseError(
                f"{pin.repository} {release.tag} assets differ from the digests pinned for "
                f"{pin.name}; check the release and the pins file."
            )
        print(f"{pin.name} is already pinned to {format_version(version)}; digests match.")
        return 0
    updated = replace(
        pin,
        version=version,
        commit=context.github.commit(pin.repository, release.tag),
        assets=assets,
    )
    files = {
        reference.file: _reference_path(context.root, reference.file)
        for reference in pin.references
    }
    try:
        texts = {file: path.read_text(encoding="utf-8") for file, path in files.items()}
    except (OSError, UnicodeError) as error:
        raise ReleaseError(f"Cannot read a reference file: {error}") from error
    rewritten = rewrite_references(texts, pin.references, pin.version, version)
    content = context.render(
        tuple(updated if item.name == pin.name else item for item in context.pins)
    )
    for file, text in rewritten.items():
        _write_text(files[file], text)
    _write_text(context.path, content)
    print(f"Pinned {pin.name} {format_version(pin.version)} -> {format_version(version)}.")
    _run_after_update(context, updated)
    changed = [context.relative(context.path), *files]
    print(f"Review and commit: {', '.join(changed)}")
    return 0


def _add(context: _Context, args: argparse.Namespace) -> int:
    if not PIN_NAME.fullmatch(args.name):
        raise ReleaseError("Pin names use lowercase letters, digits, '-' and '_'.")
    if any(pin.name == args.name for pin in context.pins):
        raise ReleaseError(f"A pin named {args.name!r} already exists; use 'curtains pins update'.")
    if not REPOSITORY.fullmatch(args.repository):
        raise ReleaseError(f"Expected owner/name, got {args.repository!r}.")
    validate_ref(pin_tag(args.tag_prefix, (0,)), "--tag-prefix")
    for asset in args.assets:
        validate_asset_template(asset, (0,), f"asset {asset!r}")
    tag = None if args.version is None else requested_tag(args.tag_prefix, args.version)
    release = context.github.release(args.repository, tag)
    version = release_version(args.repository, args.tag_prefix, release.tag)
    pin = Pin(
        name=args.name,
        repository=args.repository,
        tag_prefix=args.tag_prefix,
        version=version,
        commit=context.github.commit(args.repository, release.tag),
        assets=_release_assets(context.github, args.repository, args.assets, release, version),
        references=(),
        after_update=(),
    )
    _write_text(context.path, context.render((*context.pins, pin)))
    print(
        f"Pinned {pin.name} to {args.repository} {release.tag} in {context.relative(context.path)}."
    )
    return 0


def _fetch(context: _Context, args: argparse.Namespace) -> int:
    pin = context.find(args.name)
    tag = pin_tag(pin.tag_prefix, pin.version)
    names = {asset_name(asset.name, pin.version): asset for asset in pin.assets}
    unknown = sorted(set(args.asset) - names.keys())
    if unknown:
        raise ReleaseError(f"{pin.name} pins no asset {name_list(unknown)}.")
    selected = [name for name in names if not args.asset or name in args.asset]
    output = (args.directory / args.output).absolute()
    try:
        output.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ReleaseError(f"Cannot create {output}: {error}") from error
    with tempfile.TemporaryDirectory(dir=output, prefix=".curtains-fetch-") as scratch:
        for name in selected:
            path = context.github.download(pin.repository, tag, name, Path(scratch))
            if file_digest(path) != names[name].digest:
                raise ReleaseError(f"{name} from {pin.repository} {tag} does not match its pin.")
            os.replace(path, output / name)
            print(f"Fetched {name} ({pin.repository} {tag}, sha256 verified)")
    return 0


def _location_arguments(parser: argparse.ArgumentParser, *, defaults: bool = True) -> None:
    add_location_arguments(
        parser,
        defaults=defaults,
        file_option="--file",
        file_help=f"Pins JSON, relative to the starting directory (default: {DEFAULT_FILE})",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="curtains pins",
        description="Pin, check, update and fetch other repositories' GitHub release assets.",
    )
    _location_arguments(parser)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    check = commands.add_parser(
        "check",
        help="Report pins behind their repository's latest release",
        description="Report pins behind their repository's latest release.",
    )
    _location_arguments(check, defaults=False)
    check.add_argument("names", nargs="*", metavar="NAME")
    check.add_argument(
        "--strict", action="store_true", help="Exit 1 when a pin is behind or cannot be checked"
    )
    check.add_argument("--json", action="store_true", help="Print a JSON report")
    check.set_defaults(handler=_check)
    update = commands.add_parser(
        "update",
        help="Pin the latest (or given) release and run the pin's follow-up commands",
        description=(
            "Pin the latest or given release: record asset digests and the tag commit, "
            "rewrite references, then run the pin's after_update commands."
        ),
    )
    _location_arguments(update, defaults=False)
    update.add_argument("name", metavar="NAME")
    update.add_argument("version", nargs="?", metavar="VERSION")
    update.set_defaults(handler=_update)
    add = commands.add_parser(
        "add",
        help="Add a pin for REPOSITORY's latest (or given) release",
        description="Add a pin; ASSET names may contain {version}.",
    )
    _location_arguments(add, defaults=False)
    add.add_argument("name", metavar="NAME")
    add.add_argument("repository", metavar="OWNER/NAME")
    add.add_argument("assets", nargs="+", metavar="ASSET")
    add.add_argument("--version", help="Release version (default: latest)")
    add.add_argument("--tag-prefix", default="v", help="Release tag prefix (default: v)")
    add.set_defaults(handler=_add)
    fetch = commands.add_parser(
        "fetch",
        help="Download pinned assets and verify them against the pinned digests",
        description=(
            "Download pinned assets into OUTPUT and verify them against the pinned digests. "
            "A relative OUTPUT resolves against --directory."
        ),
    )
    _location_arguments(fetch, defaults=False)
    fetch.add_argument("name", metavar="NAME")
    fetch.add_argument("--output", required=True, type=Path, metavar="OUTPUT")
    fetch.add_argument(
        "--asset", action="append", default=[], metavar="ASSET", help="Fetch only this asset"
    )
    fetch.set_defaults(handler=_fetch)
    return parser


def main(argv: Sequence[str]) -> int:
    args = _parser().parse_args(argv)
    try:
        environment = command_environment(os.environ)
        location = resolve_location(
            args.directory, args.file, environment, default=DEFAULT_FILE, label="pins file"
        )
        context = _Context(
            location.root,
            location.config_path,
            load_pins(location.config_path),
            _GitHub(
                location.root,
                {**environment, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
            ),
            environment,
        )
        code: int = args.handler(context, args)
    except KeyboardInterrupt:
        print("curtains: interrupted", file=sys.stderr)
        return 130
    except ReleaseError as error:
        print(f"curtains: {error}", file=sys.stderr)
        return 1
    return code
