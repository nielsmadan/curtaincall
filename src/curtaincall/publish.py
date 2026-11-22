import hashlib
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from curtaincall.config import ASSET_NAME
from curtaincall.errors import ReleaseError
from curtaincall.git import (
    OBJECT_ID,
    PEELED,
    remote_tag_lines,
    require_origin_repository,
    tag_ref,
    tag_targets,
)
from curtaincall.models import ReleaseConfig
from curtaincall.process import ProcessResult, process_failure, run_process
from curtaincall.versions import require_release_tag, version_text

_GH_PATH_SYNTAX: Final = re.compile(r"[#*?\[]")
_RELEASE_FIELDS: Final = "tagName,name,body,isDraft,isImmutable,isPrerelease,assets"
_CHUNK_SIZE: Final = 1 << 20


@dataclass(frozen=True, slots=True)
class Asset:
    path: Path
    size: int
    digest: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass(frozen=True, slots=True)
class Publication:
    tag: str
    target: str
    title: str
    notes: Path
    assets: tuple[Asset, ...]
    prerelease: bool
    latest: bool


@dataclass(frozen=True, slots=True)
class _RemoteAsset:
    name: str
    uploaded: bool
    size: int | None
    digest: str | None


@dataclass(frozen=True, slots=True)
class _RemoteRelease:
    tag: str
    title: str
    body: str
    prerelease: bool
    draft: bool
    immutable: bool
    assets: tuple[_RemoteAsset, ...]


@dataclass(frozen=True, slots=True)
class _Plan:
    remove: tuple[str, ...]
    upload: tuple[Asset, ...]
    undraft: bool

    @property
    def is_complete(self) -> bool:
        return not (self.remove or self.upload or self.undraft)


def _read_asset(path: Path) -> Asset:
    if ASSET_NAME.fullmatch(path.name) is None:
        raise ReleaseError(f"Invalid release asset name: {path.name!r}")
    if _GH_PATH_SYNTAX.search(str(path)) is not None:
        raise ReleaseError(f"Release asset path must not contain '#', '*', '?' or '[': {path}")
    if not path.is_file():
        raise ReleaseError(f"Release asset does not exist: {path}")
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(_CHUNK_SIZE):
                digest.update(chunk)
                size += len(chunk)
    except OSError as error:
        raise ReleaseError(f"Cannot read release asset {path}: {error}") from error
    return Asset(path, size, f"sha256:{digest.hexdigest()}")


def read_notes(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        raise ReleaseError(f"Cannot read release notes {path}: {error}") from error


def _duplicates(names: Iterable[str]) -> list[str]:
    return sorted(name for name, count in Counter(names).items() if count > 1)


def _require_target(target: str) -> None:
    if OBJECT_ID.fullmatch(target) is None:
        raise ReleaseError(f"Release target must be a full commit SHA, got {target!r}.")


def read_assets(paths: Sequence[Path]) -> tuple[Asset, ...]:
    duplicates = _duplicates(path.name for path in paths)
    if duplicates:
        raise ReleaseError("Release asset names must be unique: " + ", ".join(duplicates))
    return tuple(_read_asset(path) for path in paths)


def default_title(config: ReleaseConfig, tag: str) -> str:
    return f"{config.name} {version_text(config, tag)}"


def _check_remote_tag(lines: Sequence[str], tag: str, target: str) -> None:
    ref = tag_ref(tag)
    refs = {name: oid for oid, _, name in (line.partition("\t") for line in lines)}
    found = tag_targets((name, oid) for name, oid in refs.items() if name in {ref, ref + PEELED})
    if tag not in found:
        raise ReleaseError(f"Release tag {tag} is not on origin.")
    if ref + PEELED not in refs:
        raise ReleaseError(f"Release tag {tag} on origin must be annotated.")
    commit = found[tag][1]
    if commit != target:
        raise ReleaseError(f"Release tag {tag} on origin identifies {commit}, not {target}.")


def _parse_asset(item: object) -> _RemoteAsset:
    if not isinstance(item, dict) or not isinstance(item.get("name"), str):
        raise ReleaseError("GitHub release asset identity is unavailable.")
    size, digest = item.get("size"), item.get("digest")
    return _RemoteAsset(
        item["name"],
        item.get("state") == "uploaded",
        size if isinstance(size, int) and not isinstance(size, bool) else None,
        digest if isinstance(digest, str) else None,
    )


def _parse_assets(raw: object) -> tuple[_RemoteAsset, ...]:
    if not isinstance(raw, list):
        raise ReleaseError("GitHub release asset identity is unavailable.")
    assets = tuple(_parse_asset(item) for item in raw)
    duplicates = _duplicates(asset.name for asset in assets)
    if duplicates:
        raise ReleaseError(f"GitHub release has duplicate asset name {duplicates[0]!r}.")
    return assets


def _parse_release(result: ProcessResult, tag: str) -> _RemoteRelease | None:
    if result.code == 1 and "release not found" in result.stderr:
        return None
    if result.code:
        raise ReleaseError(process_failure(f"gh release view {tag}", result))
    invalid = f"GitHub returned invalid release metadata for {tag}."
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ReleaseError(invalid) from error
    if not isinstance(raw, dict):
        raise ReleaseError(invalid)
    draft, immutable = raw.get("isDraft"), raw.get("isImmutable")
    if not isinstance(draft, bool) or not isinstance(immutable, bool):
        raise ReleaseError("GitHub release draft or immutability state is unavailable.")
    name, title, body, prerelease = (
        raw.get(key) for key in ("tagName", "name", "body", "isPrerelease")
    )
    if not (
        isinstance(name, str)
        and isinstance(title, str)
        and isinstance(body, str)
        and isinstance(prerelease, bool)
    ):
        raise ReleaseError(invalid)
    return _RemoteRelease(
        name, title, body, prerelease, draft, immutable, _parse_assets(raw.get("assets"))
    )


def _check_metadata(release: _RemoteRelease, publication: Publication, body: str) -> None:
    expected = (
        ("tagName", release.tag, publication.tag),
        ("name", release.title, publication.title),
        ("body", release.body, body),
    )
    for field, actual, wanted in expected:
        if actual != wanted:
            raise ReleaseError(
                f"GitHub release {field} differs from the expected {publication.tag} publication."
            )
    if release.prerelease is not publication.prerelease:
        raise ReleaseError(
            f"GitHub release prerelease state differs from the expected {publication.tag} "
            "publication."
        )


def _check_uploaded(asset: Asset, remote: _RemoteAsset) -> None:
    detail = (
        "size differs"
        if remote.size != asset.size
        else "digest is unavailable"
        if remote.digest is None
        else "digest differs"
        if remote.digest != asset.digest
        else None
    )
    if detail is not None:
        raise ReleaseError(f"GitHub release asset {asset.name!r} has divergent bytes ({detail}).")


def _release_plan(
    release: _RemoteRelease, publication: Publication, body: str, *, expect_draft: bool
) -> _Plan:
    _check_metadata(release, publication, body)
    if expect_draft and not release.draft:
        raise ReleaseError(
            f"GitHub release {publication.tag} is published; the policy expects a draft."
        )
    expected = {asset.name: asset for asset in publication.assets}
    unexpected = sorted({remote.name for remote in release.assets} - set(expected))
    if unexpected:
        raise ReleaseError("GitHub release has unexpected assets: " + ", ".join(unexpected))
    incomplete = tuple(remote.name for remote in release.assets if not remote.uploaded)
    if incomplete and not release.draft:
        raise ReleaseError(
            f"Published GitHub release has incomplete assets: {', '.join(incomplete)}."
        )
    uploaded = tuple(remote for remote in release.assets if remote.uploaded)
    for remote in uploaded:
        _check_uploaded(expected[remote.name], remote)
    present = {remote.name for remote in uploaded}
    upload = tuple(asset for asset in publication.assets if asset.name not in present)
    if upload and release.immutable:
        raise ReleaseError("GitHub release is immutable and is missing expected assets.")
    return _Plan(incomplete, upload, release.draft and not expect_draft)


def _require_remote_tag(
    root: Path, publication: Publication, environment: Mapping[str, str]
) -> None:
    lines = remote_tag_lines(root, publication.tag, environment)
    _check_remote_tag(lines, publication.tag, publication.target)


@dataclass(frozen=True, slots=True)
class _GitHub:
    root: Path
    repository: str
    environment: Mapping[str, str]

    def process(self, operation: str, *arguments: str) -> ProcessResult:
        return run_process(
            self.root,
            ("gh", "release", operation, "--repo", self.repository, *arguments),
            self.environment,
            input_data=b"",
        )

    def view(self, tag: str) -> _RemoteRelease | None:
        return _parse_release(self.process("view", "--json", _RELEASE_FIELDS, "--", tag), tag)

    def run(self, operation: str, *arguments: str) -> None:
        result = self.process(operation, *arguments)
        if result.code:
            raise ReleaseError(process_failure(f"gh release {operation}", result))


def _create(github: _GitHub, publication: Publication) -> _RemoteRelease:
    result = github.process(
        "create",
        "--draft",
        "--verify-tag",
        "--title",
        publication.title,
        "--notes-file",
        str(publication.notes),
        *(("--prerelease",) if publication.prerelease else ()),
        "--",
        publication.tag,
    )
    failure = process_failure(f"gh release create {publication.tag}", result)
    try:
        release = github.view(publication.tag)
    except ReleaseError as error:
        if result.code == 0:
            raise
        raise ReleaseError(f"{failure}; re-checking the release also failed: {error}") from error
    if release is not None:
        if result.code == 0:
            print(f"Created draft release {publication.tag}", flush=True)
        return release
    if result.code:
        raise ReleaseError(failure)
    raise ReleaseError(f"GitHub release {publication.tag} is missing after creation.")


def _require_complete(
    github: _GitHub, publication: Publication, body: str, *, expect_draft: bool, stage: str
) -> None:
    release = github.view(publication.tag)
    if (
        release is None
        or not _release_plan(release, publication, body, expect_draft=expect_draft).is_complete
    ):
        raise ReleaseError(f"GitHub release {publication.tag} is incomplete {stage}.")


def publish(
    root: Path, config: ReleaseConfig, publication: Publication, environment: Mapping[str, str]
) -> None:
    if config.repository is None:
        raise ReleaseError("Publishing requires the policy's repository.")
    if config.draft and not publication.latest:
        raise ReleaseError(
            "--not-latest has no effect for a draft policy; whoever publishes the draft "
            "chooses whether it becomes the latest release."
        )
    require_release_tag(config, publication.tag)
    _require_target(publication.target)
    require_origin_repository(root, config.repository, environment)
    _require_remote_tag(root, publication, environment)
    github = _GitHub(
        root,
        config.repository,
        {**environment, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"},
    )
    tag = publication.tag
    body = read_notes(publication.notes)
    found = github.view(tag)
    release = _create(github, publication) if found is None else found
    plan = _release_plan(release, publication, body, expect_draft=config.draft)
    for name in plan.remove:
        github.run("delete-asset", "--yes", "--", tag, name)
        print(f"Removed incomplete asset {name}", flush=True)
    if plan.upload:
        github.run("upload", "--", tag, *(str(asset.path) for asset in plan.upload))
        print(f"Uploaded {len(plan.upload)} asset(s) to {tag}", flush=True)
    if plan.undraft:
        _require_remote_tag(root, publication, environment)
        _require_complete(github, publication, body, expect_draft=True, stage="before publication")
        github.run(
            "edit",
            "--draft=false",
            *(("--prerelease",) if publication.prerelease else ()),
            *(() if publication.latest else ("--latest=false",)),
            "--",
            tag,
        )
        print(f"Published {tag}", flush=True)
    if not plan.is_complete:
        _require_complete(
            github, publication, body, expect_draft=config.draft, stage="after publication"
        )
    if config.draft:
        print(f"Draft {tag} ready for review", flush=True)
    elif found is not None and plan.is_complete:
        print(f"Release {tag} already complete", flush=True)
