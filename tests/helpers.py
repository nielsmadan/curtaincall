import json
import os
import subprocess
from pathlib import Path


def git(root: Path, environment: dict[str, str], *argv: str) -> str:
    result = subprocess.run(
        ("git", *argv), cwd=root, env=environment, check=True, capture_output=True
    )
    return os.fsdecode(result.stdout).strip()


def commit(
    root: Path, environment: dict[str, str], message: str = "fix: change", path: str = "tracked.txt"
) -> str:
    (root / path).parent.mkdir(parents=True, exist_ok=True)
    with (root / path).open("a") as stream:
        stream.write(message + "\n")
    git(root, environment, "add", "--", path)
    git(root, environment, "commit", "-m", message)
    return git(root, environment, "rev-parse", "HEAD")


def tag(
    root: Path,
    environment: dict[str, str],
    name: str = "v0.1.0",
    *,
    annotated: bool = True,
    push_branch: bool = False,
) -> str:
    git(root, environment, "tag", *(("-a", "-m", name) if annotated else ()), name)
    branch = ("HEAD:refs/heads/main",) if push_branch else ()
    git(root, environment, "push", "origin", *branch, f"refs/tags/{name}")
    return git(root, environment, "rev-parse", f"refs/tags/{name}^{{commit}}")


def write_policy(root: Path, *, name: str = "Example", **fields: object) -> Path:
    path = root / "scripts/release.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    policy = {"name": name, "initial_version": "0.1.0", **fields}
    path.write_text(json.dumps({key: value for key, value in policy.items() if value is not None}))
    return path


def executable(path: Path, content: str) -> Path:
    path.write_text(content)
    path.chmod(0o755)
    return path
