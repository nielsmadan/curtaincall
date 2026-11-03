#!/usr/bin/env python3
import hashlib
import json
import os
import select
import stat
import subprocess
import sys
from pathlib import Path

VALUED = frozenset({"--repo", "--json", "--title", "--notes-file"})


def _split(arguments: list[str]) -> tuple[dict[str, str], set[str], list[str]]:
    separator = arguments.index("--")
    values: dict[str, str] = {}
    switches: set[str] = set()
    tokens = iter(arguments[:separator])
    for token in tokens:
        if token in VALUED:
            values[token] = next(tokens)
        else:
            switches.add(token)
    return values, switches, arguments[separator + 1 :]


def _asset(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    return {
        "name": path.name,
        "size": len(data),
        "state": "uploaded",
        "digest": f"sha256:{hashlib.sha256(data).hexdigest()}",
    }


def _interaction_error() -> str | None:
    if (
        os.environ.get("GH_PROMPT_DISABLED") != "1"
        or os.environ.get("GH_NO_UPDATE_NOTIFIER") != "1"
    ):
        return "fake gh: prompts and the update notifier must be disabled"
    try:
        closed = (
            stat.S_ISFIFO(os.fstat(0).st_mode)
            and bool(select.select([0], [], [], 5)[0])
            and os.read(0, 1) == b""
        )
    except OSError:
        closed = False
    return None if closed else "fake gh: stdin must be an empty, closed pipe"


class Failure(Exception):
    def __init__(self, message: str = "HTTP 502: Bad Gateway") -> None:
        super().__init__(message)
        self.message = message


class Fake:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.state: dict[str, object] = json.loads(path.read_text()) if path.exists() else {}

    def save(self) -> None:
        self.path.write_text(json.dumps(self.state))

    def write(self, operation: str) -> None:
        self.state["writes"] = int(str(self.state.get("writes", 0))) + 1
        if operation == os.environ.get("FAKE_GH_FAIL"):
            raise Failure

    def release(self, tag: str) -> dict[str, object] | None:
        release = self.state.get("release")
        if isinstance(release, dict) and release.get("tagName") != tag:
            return None
        return release if isinstance(release, dict) else None

    def view(self, tag: str) -> int:
        error = os.environ.get("FAKE_GH_VIEW_ERROR") or (
            os.environ.get("FAKE_GH_VIEW_ERROR_AFTER_WRITE") if self.state.get("writes") else None
        )
        if error:
            print(error, file=sys.stderr)
            return 1
        raw = os.environ.get("FAKE_GH_VIEW_STDOUT")
        if raw is not None:
            print(raw)
            return 0
        release = self.release(tag)
        if release is None:
            print("release not found", file=sys.stderr)
            return 1
        print(json.dumps(release))
        return 0

    def create(self, values: dict[str, str], switches: set[str], tag: str) -> None:
        release = {
            "tagName": tag,
            "name": values["--title"],
            "body": Path(values["--notes-file"]).read_bytes().decode(),
            "isDraft": "--draft" in switches,
            "isImmutable": False,
            "isPrerelease": "--prerelease" in switches,
            "assets": [],
        }
        try:
            self.write("create")
        except Failure:
            if os.environ.get("FAKE_GH_FAIL_APPLIED"):
                self.state["release"] = release
            raise
        if not os.environ.get("FAKE_GH_CREATE_NOOP"):
            self.state["release"] = release

    def upload(self, tag: str, paths: list[str]) -> None:
        release = self.release(tag)
        if release is None:
            raise Failure("release not found")
        assets = release["assets"]
        assert isinstance(assets, list)
        for value in paths:
            path = Path(value)
            if any(asset["name"] == path.name for asset in assets):
                raise Failure
            try:
                self.write(f"upload:{path.name}")
            except Failure:
                assets.append({"name": path.name, "size": 0, "state": "starter", "digest": None})
                raise
            if os.environ.get("FAKE_GH_UPLOAD_PARTIAL"):
                assets.append({"name": path.name, "size": 0, "state": "starter", "digest": None})
            else:
                assets.append(_asset(path))

    def delete_asset(self, tag: str, name: str) -> None:
        release = self.release(tag)
        if release is None:
            raise Failure("release not found")
        assets = release["assets"]
        assert isinstance(assets, list)
        if not any(asset["name"] == name for asset in assets):
            raise Failure(f"asset {name!r} not found in release {tag!r}")
        self.write("delete-asset")
        release["assets"] = [asset for asset in assets if asset["name"] != name]

    def edit(self, switches: set[str], tag: str) -> None:
        release = self.release(tag)
        if release is None:
            raise Failure("release not found")
        self.write("edit")
        if "--draft=false" in switches and not os.environ.get("FAKE_GH_EDIT_NOOP"):
            release["isDraft"] = False
        if "--prerelease" in switches:
            release["isPrerelease"] = True


def main(argv: list[str]) -> int:
    with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
        log.write(json.dumps(argv) + "\n")
    if argv[:1] != ["release"] or "--" not in argv:
        print(f"fake gh: unsupported invocation {argv}", file=sys.stderr)
        return 2
    values, switches, positionals = _split(argv[2:])
    if "--repo" not in values:
        print("fake gh: --repo is required", file=sys.stderr)
        return 2
    interaction = _interaction_error()
    if interaction is not None:
        print(interaction, file=sys.stderr)
        return 2
    fake = Fake(Path(os.environ["FAKE_GH_STATE"]))
    operation, tag = argv[1], positionals[0]
    if operation == "view":
        return fake.view(tag)
    try:
        if operation == "create":
            fake.create(values, switches, tag)
        elif operation == "upload":
            fake.upload(tag, positionals[1:])
        elif operation == "delete-asset" and "--yes" in switches:
            fake.delete_asset(tag, positionals[1])
        elif operation == "edit":
            fake.edit(switches, tag)
        else:
            print(f"fake gh: unsupported invocation {argv}", file=sys.stderr)
            return 2
    except Failure as failure:
        fake.save()
        print(failure.message, file=sys.stderr)
        return 1
    fake.save()
    hook = os.environ.get("FAKE_GH_AFTER_UPLOAD")
    if operation == "upload" and hook:
        subprocess.run(json.loads(hook), check=True, capture_output=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
