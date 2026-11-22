#!/usr/bin/env python3
import hashlib
import json
import os
import sys
from pathlib import Path

from tests.fake_gh import interaction_error


def _state() -> dict[str, dict[str, object]]:
    repos: dict[str, dict[str, object]] = json.loads(Path(os.environ["FAKE_GH_STATE"]).read_text())
    return repos


def _options(arguments: list[str]) -> tuple[dict[str, str], list[str]]:
    separator = arguments.index("--")
    tokens = iter(arguments[:separator])
    return dict(zip(tokens, tokens, strict=True)), arguments[separator + 1 :]


def _release(repo: dict[str, object], tag: str | None) -> dict[str, object] | None:
    releases = repo["releases"]
    assert isinstance(releases, dict)
    found = releases.get(repo["latest"] if tag is None else tag)
    return found if isinstance(found, dict) else None


def _view(repo: dict[str, object], tag: str | None) -> int:
    release = _release(repo, tag)
    if release is None:
        print("release not found", file=sys.stderr)
        return 1
    contents = release["assets"]
    assert isinstance(contents, dict)
    hidden = release.get("no_digest", [])
    assert isinstance(hidden, list)
    assets = [
        {
            "name": name,
            "digest": None
            if name in hidden
            else f"sha256:{hashlib.sha256(str(content).encode()).hexdigest()}",
        }
        for name, content in contents.items()
    ]
    tag_name = repo["latest"] if tag is None else tag
    print(json.dumps({"tagName": tag_name, "isDraft": False, "assets": assets}))
    return 0


def _download(repo: dict[str, object], options: dict[str, str], tag: str) -> int:
    release = _release(repo, tag)
    assert release is not None
    contents = release["assets"]
    assert isinstance(contents, dict)
    name = options["--pattern"]
    if name not in contents:
        print("no assets match the file pattern", file=sys.stderr)
        return 1
    served = release.get("served", {})
    assert isinstance(served, dict)
    Path(options["--dir"], name).write_text(str(served.get(name, contents[name])))
    return 0


def main(argv: list[str]) -> int:
    with Path(os.environ["FAKE_GH_LOG"]).open("a") as log:
        log.write(json.dumps(argv) + "\n")
    interaction = interaction_error()
    if interaction is not None:
        print(interaction, file=sys.stderr)
        return 2
    error = os.environ.get("FAKE_GH_ERROR")
    if error:
        print(error, file=sys.stderr)
        return 1
    repos = _state()
    if argv[:1] == ["api"]:
        _, owner, name, _, tag = argv[3].split("/")
        releases = repos[f"{owner}/{name}"]["releases"]
        assert isinstance(releases, dict)
        print(releases[tag]["commit"])
        return 0
    options, positionals = _options(argv[2:])
    repo = repos[options["--repo"]]
    if argv[:2] == ["release", "view"]:
        return _view(repo, positionals[0] if positionals else None)
    if argv[:2] == ["release", "download"]:
        return _download(repo, options, positionals[0])
    print(f"fake gh: unsupported invocation {argv}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
