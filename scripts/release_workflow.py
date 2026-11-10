import argparse
import hashlib
import re
import sys
import tomllib
from pathlib import Path

SEMVER_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
FORMULA_TEMPLATE = Path(__file__).with_name("curtaincall.rb.in")
FORMULA_URL = re.compile(
    r'^  url "https://github\.com/nielsmadan/curtaincall/archive/refs/tags/(v[^"/]+)\.tar\.gz"$',
    re.MULTILINE,
)


class WorkflowError(Exception):
    pass


def version(tag: str) -> tuple[int, int, int] | None:
    match = SEMVER_TAG.fullmatch(tag)
    if match is None:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def project_metadata(tag: str, path: Path) -> str:
    if version(tag) is None:
        raise WorkflowError(f"Invalid release tag: {tag!r}")
    project = tomllib.loads(path.read_text(encoding="utf-8")).get("project")
    if not isinstance(project, dict) or project.get("name") != "curtaincall":
        raise WorkflowError("Expected the curtaincall project manifest.")
    expected_version = tag.removeprefix("v")
    if project.get("version") != expected_version:
        raise WorkflowError(
            f"Tag version {expected_version!r} does not match project version "
            f"{project.get('version')!r}."
        )
    return expected_version


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def render_formula(template: str, tag: str, sha256: str) -> str:
    if version(tag) is None:
        raise WorkflowError(f"Invalid release tag: {tag!r}")
    return template.replace("@VERSION@", tag.removeprefix("v")).replace("@SHA256@", sha256)


def formula_version(content: str) -> tuple[int, int, int]:
    tags = FORMULA_URL.findall(content)
    parsed = version(tags[0]) if len(tags) == 1 else None
    if parsed is None:
        raise WorkflowError("Formula has no unique curtaincall release version.")
    return parsed


def publish_formula(formula: Path, tap_formula: Path) -> bool:
    content = formula.read_text(encoding="utf-8")
    release = formula_version(content)
    if tap_formula.exists() and formula_version(tap_formula.read_text(encoding="utf-8")) > release:
        return False
    tap_formula.parent.mkdir(parents=True, exist_ok=True)
    tap_formula.write_text(content, encoding="utf-8")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="operation", required=True)
    metadata = subparsers.add_parser("project-metadata")
    metadata.add_argument("tag")
    metadata.add_argument("project", type=Path)
    formula = subparsers.add_parser("formula")
    formula.add_argument("tag")
    formula.add_argument("archive", type=Path)
    formula.add_argument("output", type=Path)
    publish = subparsers.add_parser("publish-formula")
    publish.add_argument("formula", type=Path)
    publish.add_argument("tap_formula", type=Path)
    args = parser.parse_args()
    if args.operation == "project-metadata":
        print(f"version={project_metadata(args.tag, args.project)}")
        return
    if args.operation == "formula":
        template = FORMULA_TEMPLATE.read_text(encoding="utf-8")
        content = render_formula(template, args.tag, _sha256(args.archive))
        args.output.write_text(content, encoding="utf-8")
        return
    if args.operation == "publish-formula":
        print("updated" if publish_formula(args.formula, args.tap_formula) else "skipped-newer")


if __name__ == "__main__":
    try:
        main()
    except (WorkflowError, OSError, UnicodeError, ValueError, KeyboardInterrupt) as error:
        print(f"Release workflow stopped: {error}", file=sys.stderr)
        sys.exit(1)
