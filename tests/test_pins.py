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
from curtaincall.config import dump_pins, load_config, parse_pins
from curtaincall.errors import ReleaseError
from curtaincall.models import Pin, PinAsset, PinReference, PinUpdate, ReleaseRequest, Stage
from curtaincall.pins import parse_release, pin_stage, release_version, rewrite_references
from curtaincall.release import release as run_release
from tests.helpers import executable, git, tag, write_policy

FAKE_GH = Path(__file__).with_name("fake_gh_releases.py")
REPOSITORY = "o/tool"
OTHER_REPOSITORY = "o/other"
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
    files=(),
    after_update=(),
)


def test_pins_round_trip_through_json() -> None:
    original = replace(
        BASE,
        tag_prefix="tool-",
        references=(PinReference("install.sh", "TOOL={version}"),),
        files=("uv.lock",),
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
        ({"files": ["../uv.lock"]}, r"tool\.files\[0\]"),
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


def configure(setup: Setup, pin: str = "tool", **fields: object) -> None:
    content = json.loads(setup.pins_file.read_text())
    content[pin].update(fields)
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


def test_update_lists_declared_files_for_review(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    (pinned.root / "install.sh").write_text("TOOL=v1.0.0\n")
    configure(
        pinned,
        references=[{"file": "install.sh", "pattern": "TOOL=v{version}"}],
        files=["install.sh", "uv.lock"],
    )
    assert pinned.run("update", "tool") == 0
    assert capsys.readouterr().out.splitlines()[-1] == (
        "Review and commit: scripts/pins.json, install.sh, uv.lock"
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


def test_pin_stage_allows_the_pins_file_references_and_declared_files() -> None:
    pin = replace(
        BASE,
        tag_prefix="tool-{x}-",
        references=(
            PinReference("install.sh", "{version}"),
            PinReference("install.sh", "v{version}"),
        ),
        files=("uv.lock",),
    )
    assert pin_stage(PinUpdate(pin, (1, 1, 0))) == Stage(
        files=("scripts/pins.json", "install.sh", "uv.lock"),
        commands=(
            (
                "{python}",
                "-m",
                "curtaincall",
                "pins",
                "update",
                "--release-stage",
                "--",
                "tool",
                "tool-{{x}}-1.1.0",
            ),
        ),
        checks=(),
        message="fix: update tool to 1.1.0",
    )


@dataclass(frozen=True, slots=True)
class Project:
    setup: Setup
    environment: dict[str, str]

    @property
    def root(self) -> Path:
        return self.setup.root

    def git(self, *arguments: str) -> str:
        return git(self.root, self.environment, *arguments)

    def commit_all(self, message: str) -> None:
        self.git("add", "--all")
        self.git("commit", "-m", message)

    def release(self, *, dry_run: bool = False, keep_pins: bool = False) -> None:
        run_release(
            self.root,
            load_config(self.root / "scripts/release.json"),
            ReleaseRequest(version=None, dry_run=dry_run, yes=True, keep_pins=keep_pins),
            self.environment,
            prompt=None,
        )

    def remote_log(self) -> list[str]:
        self.git("fetch", "--quiet", "origin")
        return self.git("log", "--format=%s", "origin/main").splitlines()

    def remote_refs(self) -> str:
        return self.git("ls-remote", "origin")


STAGES: Final = [
    {
        "files": ["tracked.txt"],
        "commands": [[sys.executable, "-c", "open('tracked.txt', 'w').write('{version}')"]],
        "message": "chore: release {version}",
    }
]
KEEP_PINS: Final = "To release without updating pins, run curtains with --keep-pins."


@pytest.fixture
def project(pinned: Setup, git_environment: dict[str, str]) -> Project:
    environment = {
        **git_environment,
        **{key: os.environ[key] for key in ("PATH", "FAKE_GH_STATE", "FAKE_GH_LOG")},
    }
    result = Project(pinned, environment)
    (pinned.root / ".gitignore").write_text("built\n")
    (pinned.root / "install.sh").write_text("TOOL=v1.0.0\n")
    built = "open('built', 'w').write('{version}')"
    configure(
        pinned,
        references=[{"file": "install.sh", "pattern": "TOOL=v{version}"}],
        after_update=[[sys.executable, "-c", built]],
    )
    write_policy(pinned.root, pins=["tool"], stages=STAGES)
    result.commit_all("chore: pin tool")
    tag(pinned.root, environment, "v0.1.0", push_branch=True)
    return result


def test_release_updates_an_outdated_pin_before_the_policy_stages(project: Project) -> None:
    project.release()
    assert project.remote_log()[:3] == [
        "chore: release 0.1.1",
        "fix: update tool to 1.1.0",
        "chore: pin tool",
    ]
    assert project.git("describe", "--tags", "main") == "v0.1.1"
    assert project.setup.stored()["version"] == "1.1.0"
    assert project.setup.stored()["commit"] == NEW_COMMIT
    assert (project.root / "install.sh").read_text() == "TOOL=v1.1.0\n"
    assert (project.root / "built").read_text() == "1.1.0"


def test_release_with_two_part_versions_bumps_minor_for_a_pin_update(project: Project) -> None:
    write_policy(project.root, pins=["tool"], version_parts=2, initial_version="0.1")
    project.commit_all("chore: use two-part versions")
    tag(project.root, project.environment, "v0.1", push_branch=True)
    project.release()
    assert project.git("describe", "--tags", "main") == "v0.2"


def test_release_with_current_pins_adds_no_pin_commit(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    assert project.setup.run("update", "tool") == 0
    project.commit_all("fix: bundle tool 1.1.0")
    capsys.readouterr()
    project.release()
    assert "Pins: current" in capsys.readouterr().out
    assert project.remote_log()[:2] == ["chore: release 0.1.1", "fix: bundle tool 1.1.0"]


def test_dry_run_previews_the_pin_update_without_writing(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    before = project.setup.pins_file.read_bytes()
    capsys.readouterr()
    project.release(dry_run=True)
    output = capsys.readouterr().out
    assert "Example: v0.1.0 -> v0.1.1" in output
    assert "Pins: tool 1.0.0 -> 1.1.0" in output
    assert project.setup.pins_file.read_bytes() == before


def test_keep_pins_releases_without_contacting_github(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "tracked.txt").write_text("fix\n")
    project.commit_all("fix: change")
    calls = project.setup.log.read_text()
    capsys.readouterr()
    project.release(keep_pins=True)
    assert "Pins: not checked (--keep-pins)" in capsys.readouterr().out
    assert project.setup.log.read_text() == calls
    assert project.remote_log()[:2] == ["chore: release 0.1.1", "fix: change"]
    assert project.setup.stored()["version"] == "1.0.0"


def test_keep_pins_with_only_a_pin_update_has_nothing_to_release(project: Project) -> None:
    with pytest.raises(ReleaseError, match="no relevant commits"):
        project.release(keep_pins=True)


def test_failed_pin_follow_up_leaves_the_update_for_review(project: Project) -> None:
    configure(project.setup, after_update=[[sys.executable, "-c", "raise SystemExit(3)"]])
    project.commit_all("chore: break the follow-up")
    refs = project.remote_refs()
    with pytest.raises(ReleaseError) as raised:
        project.release()
    message = str(raised.value)
    assert message.startswith("Updating tool to 1.1.0 failed: ")
    assert "discard the preparation changes and run curtains with --keep-pins." in message
    assert message.endswith(
        "Branch main is unchanged; uncommitted changes: 'install.sh', 'scripts/pins.json'."
    )
    assert project.setup.stored()["version"] == "1.1.0"
    assert project.remote_refs() == refs


def test_pin_follow_up_may_change_the_pin_files(project: Project) -> None:
    (project.root / "lock.txt").write_text("1.0.0\n")
    configure(
        project.setup,
        files=["lock.txt"],
        after_update=[[sys.executable, "-c", "open('lock.txt', 'w').write('{version}')"]],
    )
    project.commit_all("chore: lock the tool")
    project.release()
    changed = project.git("show", "--format=", "--name-only", "main~1").splitlines()
    assert changed == ["install.sh", "lock.txt", "scripts/pins.json"]
    assert (project.root / "lock.txt").read_text() == "1.1.0"


def test_pin_follow_up_changing_an_undeclared_file_stops_the_release(project: Project) -> None:
    (project.root / "lock.txt").write_text("1.0.0\n")
    configure(
        project.setup,
        after_update=[[sys.executable, "-c", "open('lock.txt', 'w').write('{version}')"]],
    )
    project.commit_all("chore: lock the tool")
    with pytest.raises(
        ReleaseError, match=re.escape("Preparation changed undeclared paths: 'lock.txt'")
    ):
        project.release()


def test_pin_lookup_failure_stops_the_release(project: Project) -> None:
    project.environment["FAKE_GH_ERROR"] = "HTTP 502: Bad Gateway"
    refs = project.remote_refs()
    with pytest.raises(ReleaseError) as raised:
        project.release()
    assert str(raised.value) == (
        "Cannot check pin tool: gh release view --repo o/tool failed (1): HTTP 502: Bad Gateway\n"
        + KEEP_PINS
    )
    assert project.remote_refs() == refs


def test_release_names_a_policy_pin_missing_from_the_pins_file(project: Project) -> None:
    write_policy(project.root, pins=["tool", "other"])
    project.commit_all("chore: pin other")
    with pytest.raises(ReleaseError) as raised:
        project.release()
    assert str(raised.value) == (
        f"Pins named in the policy are missing from scripts/pins.json: 'other'.\n{KEEP_PINS}"
    )


def test_suggestion_without_a_bump_still_releases_a_pin_update(project: Project) -> None:
    write_policy(
        project.root,
        pins=["tool"],
        suggest=[sys.executable, "-c", "print('0.1.0')"],
        stages=STAGES,
    )
    project.commit_all("chore: suggest versions")
    project.release()
    assert project.git("describe", "--tags", "main") == "v0.1.1"


def test_pin_update_releases_outside_the_history_paths(project: Project) -> None:
    write_policy(project.root, pins=["tool"], history_paths=["src"], stages=STAGES)
    project.commit_all("chore: scope history")
    project.release()
    assert project.remote_log()[:2] == ["chore: release 0.1.1", "fix: update tool to 1.1.0"]


@pytest.fixture
def two_pins(project: Project) -> Project:
    state = json.loads(project.setup.state.read_text())
    state[OTHER_REPOSITORY] = {
        "latest": "v2.1.0",
        "releases": {
            "v2.0.0": {"commit": OLD_COMMIT, "assets": {"other.tar.gz": "other 2.0.0"}},
            "v2.1.0": {"commit": NEW_COMMIT, "assets": {"other.tar.gz": "other 2.1.0"}},
        },
    }
    project.setup.state.write_text(json.dumps(state))
    assert (
        project.setup.run("add", "other", OTHER_REPOSITORY, "other.tar.gz", "--version", "2.0.0")
        == 0
    )
    write_policy(project.root, pins=["tool", "other"], stages=STAGES)
    project.commit_all("chore: pin other")
    return project


def test_release_updates_every_outdated_pin(
    two_pins: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    two_pins.release()
    output = capsys.readouterr().out
    assert "Pins: tool 1.0.0 -> 1.1.0; other 2.0.0 -> 2.1.0" in output
    assert "Changes: 0 feature(s), 2 fix(es) (2 from pin updates), 0 breaking change(s)" in output
    assert two_pins.remote_log()[:3] == [
        "chore: release 0.1.1",
        "fix: update other to 2.1.0",
        "fix: update tool to 1.1.0",
    ]


def test_release_counts_pin_updates_among_the_fixes(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    (project.root / "tracked.txt").write_text("fix\n")
    project.commit_all("fix: change")
    capsys.readouterr()
    project.release(dry_run=True)
    assert (
        "Changes: 0 feature(s), 2 fix(es) (1 from pin updates), 0 breaking change(s)"
        in capsys.readouterr().out
    )


def test_failed_pin_lookup_names_only_the_failed_pin(two_pins: Project) -> None:
    state = json.loads(two_pins.setup.state.read_text())
    state[OTHER_REPOSITORY]["latest"] = "v9.9.9"
    two_pins.setup.state.write_text(json.dumps(state))
    with pytest.raises(ReleaseError) as raised:
        two_pins.release()
    assert str(raised.value) == (
        f"Cannot check pin other: o/other has no published release.\n{KEEP_PINS}"
    )


def test_every_failed_pin_lookup_is_reported(two_pins: Project) -> None:
    state = json.loads(two_pins.setup.state.read_text())
    state[REPOSITORY]["latest"] = state[OTHER_REPOSITORY]["latest"] = "v9.9.9"
    two_pins.setup.state.write_text(json.dumps(state))
    with pytest.raises(ReleaseError) as raised:
        two_pins.release()
    assert str(raised.value) == (
        "Cannot check pin tool: o/tool has no published release.\n"
        "Cannot check pin other: o/other has no published release.\n" + KEEP_PINS
    )


def test_failed_later_pin_keeps_the_earlier_pin_commit(two_pins: Project) -> None:
    configure(two_pins.setup, "other", after_update=[[sys.executable, "-c", "raise SystemExit(5)"]])
    two_pins.commit_all("chore: break the other follow-up")
    refs = two_pins.remote_refs()
    with pytest.raises(ReleaseError) as raised:
        two_pins.release()
    message = str(raised.value)
    assert message.startswith("Updating other to 2.1.0 failed: ")
    assert "(1 new commit); uncommitted changes: 'scripts/pins.json'." in message
    assert two_pins.git("log", "-1", "--format=%s") == "fix: update tool to 1.1.0"
    assert two_pins.remote_refs() == refs


def test_cli_keep_pins_flag(project: Project, capsys: pytest.CaptureFixture[str]) -> None:
    (project.root / "tracked.txt").write_text("fix\n")
    project.commit_all("fix: change")
    capsys.readouterr()
    assert cli.main(["--directory", str(project.root), "--dry-run", "--keep-pins"]) == 0
    assert "Pins: not checked (--keep-pins)" in capsys.readouterr().out


def test_interactive_override_keeps_counting_the_pin_update(
    project: Project, capsys: pytest.CaptureFixture[str]
) -> None:
    answers = iter(["minor", "y"])
    capsys.readouterr()
    run_release(
        project.root,
        load_config(project.root / "scripts/release.json"),
        ReleaseRequest(version=None, dry_run=False, yes=False, keep_pins=False),
        project.environment,
        prompt=lambda _: next(answers),
    )
    output = capsys.readouterr().out
    expected = "Changes: 0 feature(s), 1 fix(es) (1 from pin updates), 0 breaking change(s)"
    assert output.count(expected) == 2
    assert output.count("Pins: tool 1.0.0 -> 1.1.0") == 2
    assert project.remote_log()[:2] == ["chore: release 0.2.0", "fix: update tool to 1.1.0"]


def test_release_stage_update_reports_only_the_failed_command(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(pinned, after_update=[[sys.executable, "-c", "raise SystemExit(4)"]])
    assert pinned.run("update", "--release-stage", "--", "tool", "v1.1.0") == 1
    assert capsys.readouterr().err.endswith("-c 'raise SystemExit(4)' failed (4)\n")


def test_release_stage_update_prints_no_commit_advice(
    pinned: Setup, capsys: pytest.CaptureFixture[str]
) -> None:
    capsys.readouterr()
    assert pinned.run("update", "--release-stage", "--", "tool", "v1.1.0") == 0
    assert capsys.readouterr().out == "Pinned tool 1.0.0 -> 1.1.0.\n"
