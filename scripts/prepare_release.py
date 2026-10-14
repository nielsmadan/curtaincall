import argparse
import re
import tomllib
from pathlib import Path


def prepare_release(root: Path, version: str) -> None:
    if re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version) is None:
        raise ValueError("Expected a canonical MAJOR.MINOR.PATCH version.")
    manifest = root / "pyproject.toml"
    source = manifest.read_text(encoding="utf-8")
    parsed = tomllib.loads(source)
    matches = tuple(
        re.finditer(r"(?m)^[ \t]*version[ \t]*=[ \t]*([\"'])([^\r\n]*?)\1[ \t]*(?:#.*)?$", source)
    )
    project = parsed.get("project", {})
    if (
        not isinstance(project, dict)
        or len(matches) != 1
        or project.get("version") != matches[0][2]
    ):
        raise ValueError("Expected exactly one manifest version, in [project].")
    start, end = matches[0].span(2)
    manifest.write_text(source[:start] + version + source[end:], encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Update the validated package version.")
    parser.add_argument("version")
    args = parser.parse_args()
    try:
        prepare_release(Path(__file__).resolve().parent.parent, args.version)
    except (OSError, ValueError) as error:
        parser.exit(1, f"prepare-release: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
