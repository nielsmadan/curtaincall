import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ENTRY_POINT = re.compile(r"([A-Za-z_][\w.]*):([A-Za-z_]\w*)")


def project_metadata(source: Path) -> tuple[str, str, str]:
    with (source / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle).get("project")
    project = project if isinstance(project, dict) else {}
    value = project.get("version")
    if not isinstance(value, str) or not value:
        raise ValueError("pyproject.toml [project].version must be a non-empty string")
    scripts = project.get("scripts")
    entry = scripts.get("curtains") if isinstance(scripts, dict) else None
    match = ENTRY_POINT.fullmatch(entry) if isinstance(entry, str) else None
    if match is None:
        raise ValueError("pyproject.toml [project.scripts].curtains must be MODULE:FUNCTION")
    return value, match[1], match[2]


def launcher_text(executable: str, source: Path, site: Path, module: str, function: str) -> str:
    return (
        f"#!{executable} -I\n"
        "import sys\n"
        f"sys.path[:0] = [{str(source / 'src')!r}, {str(site)!r}]\n"
        f"from {module} import {function}\n"
        f"sys.exit({function}())\n"
    )


def _write_metadata(site: Path, version: str) -> None:
    for stale in site.glob("curtaincall-*.dist-info"):
        shutil.rmtree(stale)
    metadata = site / f"curtaincall-{version}.dist-info" / "METADATA"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        f"Metadata-Version: 2.1\nName: curtaincall\nVersion: {version}\n", encoding="utf-8"
    )


def _verify_launcher(launcher: Path, version: str) -> None:
    result = subprocess.run(
        (str(launcher), "--version"), capture_output=True, text=True, check=False
    )
    if result.returncode != 0 or result.stdout.strip() != f"curtains {version}":
        raise RuntimeError(f"installed launcher failed: {result.stdout}{result.stderr}")


def install(source: Path, destination: Path) -> None:
    version, module, function = project_metadata(source)
    site = destination / "site"
    _write_metadata(site, version)
    launcher = destination / "bin" / "curtains"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        launcher_text(sys.executable, source, site, module, function), encoding="utf-8"
    )
    launcher.chmod(0o755)
    _verify_launcher(launcher, version)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: install_action.py DEST", file=sys.stderr)
        return 2
    try:
        install(Path(__file__).resolve().parent.parent, Path(argv[0]).resolve())
    except (OSError, ValueError, RuntimeError, tomllib.TOMLDecodeError) as error:
        print(f"install_action: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
