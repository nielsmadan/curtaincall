from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ready: bool
    hint: str


def command(root: Path, environment: Mapping[str, str], argv: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(
            argv, cwd=root, env=environment, capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def git_version_ready(value: str | None) -> bool:
    match = re.match(r"git version (\d+)\.(\d+)(?:\.|\s|$)", value or "")
    return match is not None and tuple(map(int, match.groups())) >= (2, 45)


def hook_check(root: Path, environment: Mapping[str, str], git: str | None, name: str) -> Check:
    hook_path = (
        command(root, environment, (git, "rev-parse", "--git-path", f"hooks/{name}"))
        if git
        else None
    )
    return Check(
        name,
        hook_path is not None and executable(root / hook_path),
        "run: just setup (hook must be an executable file)",
    )


def inspect(root: Path, environment: Mapping[str, str]) -> tuple[Check, ...]:
    tools = (
        ("git", "install Git >=2.45: https://git-scm.com/downloads"),
        ("uv", "install: https://docs.astral.sh/uv/getting-started/installation/"),
        ("just", "install: https://github.com/casey/just#installation"),
        ("lefthook", "install: https://lefthook.dev/installation/"),
    )
    search_path = os.pathsep.join(
        str(root / part) for part in environment.get("PATH", os.defpath).split(os.pathsep)
    )
    available = {name: shutil.which(name, path=search_path) for name, _hint in tools}
    checks = tuple(Check(name, available[name] is not None, hint) for name, hint in tools)
    checks += (
        Check(
            "Git >=2.45",
            git_version_ready(command(root, environment, (available["git"], "--version")))
            if available["git"]
            else False,
            "upgrade Git: read-only inspection requires --no-lazy-fetch (Git >=2.45)",
        ),
        Check(
            ".venv",
            executable(root / ".venv/bin/python")
            and command(
                root,
                environment,
                (
                    str(root / ".venv/bin/python"),
                    "-c",
                    "import sys; raise SystemExit(sys.version_info < (3, 13))",
                ),
            )
            is not None,
            "run: uv sync --locked --all-groups (Python >=3.13)",
        ),
    )
    checks += tuple(
        Check(name, executable(root / ".venv/bin" / name), "run: uv sync --locked --all-groups")
        for name in ("pytest", "ruff", "pylint", "mypy")
    )
    checks += (
        Check(
            "Lefthook configuration",
            (root / "lefthook.yml").is_file()
            and available["lefthook"] is not None
            and command(root, environment, (available["lefthook"], "validate")) is not None,
            "check lefthook.yml with: lefthook validate",
        ),
    )
    return checks + tuple(
        hook_check(root, environment, available["git"], name) for name in ("pre-commit", "pre-push")
    )


def main() -> int:
    checks = inspect(Path(__file__).resolve().parent.parent, os.environ)
    for check in checks:
        print(
            f"  ok       {check.name}" if check.ready else f"  MISSING  {check.name}: {check.hint}"
        )
    ready = all(check.ready for check in checks)
    if ready:
        print("Everything in place.")
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
