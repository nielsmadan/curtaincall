import os
import re
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from curtaincall.errors import ReleaseError
from curtaincall.models import Command

_PRESERVED_GIT_VARIABLES: Final = frozenset(
    {
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_ALLOW_PROTOCOL",
        "GIT_PROTOCOL_FROM_USER",
        "GIT_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
        "GIT_SSH_VARIANT",
        "GIT_TERMINAL_PROMPT",
        "GIT_PROXY_COMMAND",
        "GIT_CURL_FTP_NO_EPSV",
        "GIT_GSS_DELEGATION",
    }
)


def command_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in source.items()
        if not key.startswith("GIT_")
        or key in _PRESERVED_GIT_VARIABLES
        or key.startswith(("GIT_SSL_", "GIT_PROXY_SSL_", "GIT_HTTP_"))
    }


def gh_environment(environment: Mapping[str, str]) -> dict[str, str]:
    return {**environment, "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"}


def redact_urls(value: str) -> str:
    return re.sub(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s]*@", r"\1[redacted]@", value)


@dataclass(frozen=True, slots=True)
class ProcessResult:
    code: int
    stdout: str
    stderr: str


def process_failure(what: str, result: ProcessResult) -> str:
    detail = (result.stderr or result.stdout).strip()
    return redact_urls(f"{what} failed ({result.code})" + (f": {detail}" if detail else ""))


def _spawn(
    root: Path,
    argv: Command,
    environment: Mapping[str, str],
    *,
    capture: bool,
    input_data: bytes | None,
    display: str,
) -> ProcessResult:
    try:
        result = subprocess.run(
            argv, cwd=root, env=environment, capture_output=capture, check=False, input=input_data
        )
    except (OSError, ValueError) as error:
        raise ReleaseError(redact_urls(f"Cannot run {display}: {error}")) from error
    return ProcessResult(
        result.returncode, os.fsdecode(result.stdout or b""), os.fsdecode(result.stderr or b"")
    )


def run_process(
    root: Path,
    argv: Command,
    environment: Mapping[str, str],
    *,
    input_data: bytes | None = None,
    display: str | None = None,
) -> ProcessResult:
    return _spawn(
        root,
        argv,
        environment,
        capture=True,
        input_data=input_data,
        display=display or shlex.join(argv),
    )


def run_command(
    root: Path,
    argv: Command,
    environment: Mapping[str, str],
    *,
    capture: bool = True,
    input_data: bytes | None = None,
    display: str | None = None,
) -> str:
    shown = display or shlex.join(argv)
    result = _spawn(root, argv, environment, capture=capture, input_data=input_data, display=shown)
    if result.code:
        raise ReleaseError(process_failure(shown, result))
    return result.stdout
