import hashlib
import json
import os
import re
import shlex
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

import pytest

from curtaincall import cli, pins
from curtaincall.config import dump_pins, parse_pins
from curtaincall.errors import ReleaseError
from curtaincall.models import Pin, PinAsset, PinReference
from curtaincall.pins import parse_release, release_version, rewrite_references
from tests.helpers import executable

FAKE_GH = Path(__file__).with_name("fake_gh_releases.py")
REPOSITORY = "o/tool"
OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40


def digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"


BASE: Final = Pin(
    name="tool",
    repository=REPOSITORY,
    tag_prefix="v",
    version=(1, 0, 0),
    commit=OLD_COMMIT,
    assets=(PinAsset("tool-{version}.tar.gz", digest("tool 1.0.0")),),
    references=(),
    after_update=(),
)


def test_pins_round_trip_through_json() -> None:
    original = replace(
        BASE,
        tag_prefix="tool-",
        references=(PinReference("install.sh", "TOOL={version}"),),
        after_update=(("just", "build", "{tag}"),),
    )
    assert parse_pins(dump_pins((original,)), "test") == (original,)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"assets": {"tool": "sha256:abc"}}, "expected sha256:"),
        ({"assets": {"tool-{tag}": digest("x")}}, "only plain version fields"),
        ({"assets": {}}, "at least one asset"),
        ({"commit": "main"}, "full lowercase commit SHA"),
        ({"references": [{"file": "a", "pattern": "TOOL"}]}, re.escape("must contain {version}")),
        ({"references": [{"file": "../a", "pattern": "{version}"}]}, "repository-relative"),
        ({"after_update": [["echo", "{revision}"]]}, "only plain version and tag fields"),
        ({"extra": 1}, "unknown keys: extra"),
    ],
)
def test_parse_pins_rejects_invalid_entries(entry: dict[str, object], message: str) -> None:
    valid = json.loads(dump_pins((BASE,)))["tool"]
    with pytest.raises(ReleaseError, match=message):
        parse_pins(json.dumps({"tool": {**valid, **entry}}), "test")


def test_parse_pins_rejects_invalid_names() -> None:
    valid = json.loads(dump_pins((BASE,)))["tool"]
    with pytest.raises(ReleaseError, match="pin names use lowercase"):
        parse_pins(json.dumps({"Tool": valid}), "test")


def test_rewrite_references_replaces_every_occurrence() -> None:
    references = (PinReference("a", "TOOL={version}"), PinReference("b", '"v{version}"'))
    texts = {"a": "TOOL=1.0.0\nTOOL=1.0.0\n", "b": '{"pin": "v1.0.0"}'}
    assert rewrite_references(texts, references, (1, 0, 0), (1, 2, 0)) == {
        "a": "TOOL=1.2.0\nTOOL=1.2.0\n",
        "b": '{"pin": "v1.2.0"}',
    }


def test_rewrite_references_requires_the_old_value() -> None:
    with pytest.raises(ReleaseError, match=re.escape("does not contain 'TOOL=1.0.0'")):
        rewrite_references(
            {"a": "TOOL=0.9.0"}, (PinReference("a", "TOOL={version}"),), (1, 0, 0), (1, 1, 0)
        )


def test_release_version_requires_prefix_and_numeric_version() -> None:
    assert release_version(REPOSITORY, "v", "v1.2.3") == (1, 2, 3)
    with pytest.raises(ReleaseError, match="does not start with 'v'"):
        release_version(REPOSITORY, "v", "1.2.3")
    with pytest.raises(ReleaseError, match="not a numeric release"):
        release_version(REPOSITORY, "v", "v1.2.3-rc.1")


def test_parse_release_maps_unusable_digests_to_none() -> None:
    stdout = json.dumps(
        {
            "tagName": "v1.0.0",
            "isDraft": False,
            "assets": [
                {"name": "a", "digest": digest("a")},
                {"name": "b", "digest": None},
                {"name": "c", "digest": "sha512:00"},
            ],
        }
    )
    release = parse_release(stdout, REPOSITORY)
    assert dict(release.digests) == {"a": digest("a"), "b": None, "c": None}


def test_parse_release_rejects_drafts() -> None:
    stdout = json.dumps({"tagName": "v1.0.0", "isDraft": True, "assets": []})
    with pytest.raises(ReleaseError, match="not a published release"):
        parse_release(stdout, REPOSITORY)


@dataclass(frozen=True, slots=True)
class Setup:
    root: Path
    state: Path
    log: Path

    @property
    def pins_file(self) -> Path:
        return self.root / "scripts/pins.json"

    def releases(self, releases: dict[str, dict[str, object]], latest: str) -> None:
        self.state.write_text(json.dumps({REPOSITORY: {"latest": latest, "releases": releases}}))

    def run(self, *arguments: str) -> int:
        return pins.main(["--directory", str(self.root), *arguments])

    def stored(self) -> dict[str, object]:
        loaded: dict[str, object] = json.loads(self.pins_file.read_text())["tool"]
        return loaded


def release(version: str, commit: str, **extra: object) -> dict[str, object]:
    return {"commit": commit, "assets": {f"tool-{version}.tar.gz": f"tool {version}"}, **extra}


@pytest.fixture
def setup(tmp_path: Path, repository: Path, monkeypatch: pytest.MonkeyPatch) -> Setup:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    executable(
        bin_directory / "gh",
        f"#!/bin/sh\nPYTHONPATH={shlex.quote(str(FAKE_GH.parent.parent))} "
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(FAKE_GH))} "$@"\n',
    )
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(tmp_path / "gh.json"))
    monkeypatch.setenv("FAKE_GH_LOG", str(tmp_path / "gh.log"))
    result = Setup(repository, tmp_path / "gh.json", tmp_path / "gh.log")
    result.releases(
        {"v1.0.0": release("1.0.0", OLD_COMMIT), "v1.1.0": release("1.1.0", NEW_COMMIT)},
        latest="v1.1.0",
    )
    return result


@pytest.fixture
def pinned(setup: Setup) -> Setup:
    assert setup.run("add", "tool", REPOSITORY, "tool-{version}.tar.gz", "--version", "1.0.0") == 0
    return setup


def configure(setup: Setup, **fields: object) -> None:
    content = json.loads(setup.pins_file.read_text())
    content["tool"].update(fields)
    setup.pins_file.write_text(json.dumps(content))


def test_add_records_version_commit_and_digests(pinned: Setup) -> None:
    assert pinned.stored() == {
        "repository": REPOSITORY,
        "version": "1.0.0",
        "commit": OLD_COMMIT,
        "assets": {"tool-{version}.tar.gz": digest("tool 1.0.0")},
    }


def test_add_defaults_to_the_latest_release(setup: Setup) -> None:
    assert setup.run("add", "tool", REPOSITORY, "tool-{version}.tar.gz") == 0
    assert setup.stored()["version"] == "1.1.0"


def test_add_refuses_an_existing_name(pinned: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert pinned.run("add", "tool", REPOSITORY, "x") == 1
    assert "already exists" in capsys.readouterr().err


def test_add_rejects_an_invalid_tag_prefix_before_calling_github(
    setup: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert setup.run("add", "tool", REPOSITORY, "tool.tar.gz", "--tag-prefix", "bad prefix") == 1
    assert "--tag-prefix: invalid Git ref name" in capsys.readouterr().err
    assert not setup.log.exists()


def test_check_reports_outdated_pins(pinned: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert pinned.run("check") == 0
    assert capsys.readouterr().out == (
        "tool 1.0.0: 1.1.0 is available; run: curtains pins update tool\n"
    )
    assert pinned.run("check", "--strict") == 1


def test_check_json_report(pinned: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert pinned.run("check", "--json") == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "name": "tool",
            "repository": REPOSITORY,
            "pinned": "1.0.0",
            "latest": "1.1.0",
            "outdated": True,
            "error": None,
        }
    ]


def test_check_lookup_failure_warns_unless_strict(
    pinned: Setup, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FAKE_GH_ERROR", "HTTP 502: Bad Gateway")
    assert pinned.run("check") == 0
    assert "could not check o/tool" in capsys.readouterr().err
    assert pinned.run("check", "--strict") == 1


def test_check_current_pin(pinned: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert pinned.run("update", "tool") == 0
    capsys.readouterr()
    assert pinned.run("check", "--strict") == 0
    assert capsys.readouterr().out == "tool 1.1.0: current (latest release 1.1.0)\n"


def test_update_rewrites_pin_and_references_then_runs_follow_up(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    (pinned.root / "install.sh").write_text("TOOL=v1.0.0\n")
    marker = pinned.root / "follow-up.txt"
    configure(
        pinned,
        references=[{"file": "install.sh", "pattern": "TOOL=v{version}"}],
        after_update=[[sys.executable, "-c", f"open({str(marker)!r}, 'w').write('{{tag}}')"]],
    )
    assert pinned.run("update", "tool") == 0
    assert pinned.stored()["version"] == "1.1.0"
    assert pinned.stored()["commit"] == NEW_COMMIT
    assert pinned.stored()["assets"] == {"tool-{version}.tar.gz": digest("tool 1.1.0")}
    assert (pinned.root / "install.sh").read_text() == "TOOL=v1.1.0\n"
    assert marker.read_text() == "v1.1.0"
    assert capsys.readouterr().out.splitlines()[-1] == (
        "Review and commit: scripts/pins.json, install.sh"
    )


def test_update_when_current_leaves_files_alone(pinned: Setup) -> None:
    assert pinned.run("update", "tool") == 0
    before = pinned.pins_file.read_bytes()
    assert pinned.run("update", "tool") == 0
    assert pinned.pins_file.read_bytes() == before


def test_update_never_downgrades_to_an_older_latest_release(pinned: Setup) -> None:
    assert pinned.run("update", "tool", "1.1.0") == 0
    pinned.releases(
        {"v1.0.0": release("1.0.0", OLD_COMMIT), "v1.1.0": release("1.1.0", NEW_COMMIT)},
        latest="v1.0.0",
    )
    assert pinned.run("update", "tool") == 0
    assert pinned.stored()["version"] == "1.1.0"


def test_update_to_an_explicit_older_version(pinned: Setup) -> None:
    assert pinned.run("update", "tool") == 0
    assert pinned.run("update", "tool", "v1.0.0") == 0
    assert pinned.stored()["version"] == "1.0.0"


def test_update_same_version_detects_changed_assets(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned.releases(
        {"v1.0.0": {"commit": OLD_COMMIT, "assets": {"tool-1.0.0.tar.gz": "replaced"}}},
        latest="v1.0.0",
    )
    assert pinned.run("update", "tool", "1.0.0") == 1
    assert "differ from the digests pinned" in capsys.readouterr().err


def test_update_missing_reference_changes_nothing(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    (pinned.root / "install.sh").write_text("TOOL=v0.9.0\n")
    configure(pinned, references=[{"file": "install.sh", "pattern": "TOOL=v{version}"}])
    before = pinned.pins_file.read_bytes()
    assert pinned.run("update", "tool") == 1
    assert "install.sh does not contain 'TOOL=v1.0.0'" in capsys.readouterr().err
    assert pinned.pins_file.read_bytes() == before


def test_update_reports_failed_follow_up_after_pinning(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(pinned, after_update=[[sys.executable, "-c", "raise SystemExit(4)"]])
    assert pinned.run("update", "tool") == 1
    assert "Pinned tool 1.1.0, but a follow-up command failed" in capsys.readouterr().err
    assert pinned.stored()["version"] == "1.1.0"


def test_update_hashes_downloads_when_github_has_no_digest(pinned: Setup) -> None:
    pinned.releases(
        {
            "v1.0.0": release("1.0.0", OLD_COMMIT),
            "v1.1.0": release("1.1.0", NEW_COMMIT, no_digest=["tool-1.1.0.tar.gz"]),
        },
        latest="v1.1.0",
    )
    assert pinned.run("update", "tool") == 0
    assert pinned.stored()["assets"] == {"tool-{version}.tar.gz": digest("tool 1.1.0")}


def test_update_requires_every_pinned_asset(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned.releases(
        {"v1.0.0": release("1.0.0", OLD_COMMIT), "v1.1.0": {"commit": NEW_COMMIT, "assets": {}}},
        latest="v1.1.0",
    )
    assert pinned.run("update", "tool") == 1
    assert "has no asset tool-1.1.0.tar.gz" in capsys.readouterr().err


def test_update_to_a_missing_release_names_it(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pinned.run("update", "tool", "9.9.9") == 1
    assert capsys.readouterr().err == "curtains: o/tool has no release v9.9.9.\n"


def test_update_refuses_a_reference_symlinked_outside_the_worktree(
    pinned: Setup, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outside = tmp_path / "outside.sh"
    outside.write_text("TOOL=v1.0.0\n")
    (pinned.root / "install.sh").symlink_to(outside)
    configure(pinned, references=[{"file": "install.sh", "pattern": "TOOL=v{version}"}])
    assert pinned.run("update", "tool") == 1
    assert "Reference 'install.sh' resolves outside the worktree." in capsys.readouterr().err
    assert outside.read_text() == "TOOL=v1.0.0\n"
    assert pinned.stored()["version"] == "1.0.0"


def test_update_unknown_pin_is_named(pinned: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    assert pinned.run("update", "other") == 1
    assert "No pin named 'other'" in capsys.readouterr().err


def test_fetch_places_verified_assets(pinned: Setup) -> None:
    assert pinned.run("fetch", "tool", "--output", "vendor") == 0
    assert (pinned.root / "vendor/tool-1.0.0.tar.gz").read_text() == "tool 1.0.0"


def test_fetch_rejects_bytes_that_differ_from_the_pin(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned.releases(
        {"v1.0.0": release("1.0.0", OLD_COMMIT, served={"tool-1.0.0.tar.gz": "tampered"})},
        latest="v1.0.0",
    )
    assert pinned.run("fetch", "tool", "--output", "vendor") == 1
    assert "does not match its pin" in capsys.readouterr().err
    assert list((pinned.root / "vendor").iterdir()) == []


def test_fetch_rejects_unpinned_asset_names(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    assert pinned.run("fetch", "tool", "--output", "vendor", "--asset", "other") == 1
    assert "pins no asset other" in capsys.readouterr().err


def test_cli_routes_pins(pinned: Setup, capsys: pytest.CaptureFixture[str]) -> None:
    capsys.readouterr()
    assert cli.main(["pins", "--directory", str(pinned.root), "check"]) == 0
    assert "1.1.0 is available" in capsys.readouterr().out
