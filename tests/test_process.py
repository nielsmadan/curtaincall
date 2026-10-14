import os
import subprocess
import sys
from pathlib import Path

import pytest

from curtaincall.errors import ReleaseError
from curtaincall.git import command_environment, run_command


def test_environment_removes_git_context_and_preserves_authentication() -> None:
    source = {
        "GIT_DIR": "elsewhere",
        "GIT_WORK_TREE": "elsewhere",
        "GIT_INDEX_FILE": "index",
        "GIT_OBJECT_DIRECTORY": "objects",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": "objects",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.bare",
        "GIT_CONFIG_VALUE_0": "true",
        "GIT_CONFIG_PARAMETERS": "bad",
        "GIT_REPLACE_REF_BASE": "refs/evil/",
        "GIT_LITERAL_PATHSPECS": "0",
        "GIT_NAMESPACE": "other",
        "GIT_CONFIG_GLOBAL": "credentials-config",
        "GIT_CONFIG_SYSTEM": "system-config",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ASKPASS": "auth",
        "GIT_SSH_COMMAND": "ssh auth",
        "SSH_AUTH_SOCK": "sock",
        "UV_CACHE_DIR": "mine",
        "PATH": "bin",
    }
    assert command_environment(source) == {
        "GIT_CONFIG_GLOBAL": "credentials-config",
        "GIT_CONFIG_SYSTEM": "system-config",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ALLOW_PROTOCOL": "file",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_ASKPASS": "auth",
        "GIT_SSH_COMMAND": "ssh auth",
        "SSH_AUTH_SOCK": "sock",
        "UV_CACHE_DIR": "mine",
        "PATH": "bin",
    }
    assert source["GIT_DIR"] == "elsewhere"


def test_capture_preserves_binary_and_newline_bytes(tmp_path: Path) -> None:
    output = run_command(
        tmp_path,
        (sys.executable, "-c", "import os; os.write(1, b'a\\r\\nb\\r\\xff\\0')"),
        os.environ,
    )
    assert os.fsencode(output) == b"a\r\nb\r\xff\0"


def test_environment_preserves_proxy_authentication_and_tls_controls() -> None:
    source = {
        "GIT_PROXY_SSL_CERT": "proxy.p12",
        "GIT_PROXY_SSL_KEY": "proxy.key",
        "GIT_PROXY_SSL_CAINFO": "proxy-ca.pem",
        "GIT_PROXY_SSL_CERT_PASSWORD_PROTECTED": "true",
        "GIT_PROXY_SSL_VERIFY": "true",
        "GIT_SSL_CERT": "client.p12",
        "GIT_SSL_CERT_TYPE": "P12",
        "GIT_SSL_KEY_TYPE": "PEM",
        "GIT_SSL_VERSION": "tlsv1.3",
        "GIT_SSL_CIPHER_LIST": "TLS_AES_256_GCM_SHA384",
        "GIT_HTTP_LOW_SPEED_LIMIT": "1000",
        "GIT_HTTP_LOW_SPEED_TIME": "30",
        "GIT_HTTP_PROXY_AUTHMETHOD": "negotiate",
        "GIT_CURL_FTP_NO_EPSV": "1",
        "GIT_GSS_DELEGATION": "none",
    }
    assert command_environment(source) == source


def test_argv_is_literal_and_cwd_is_selected(tmp_path: Path) -> None:
    output = run_command(
        tmp_path,
        (
            sys.executable,
            "-c",
            "import os, sys; print(os.getcwd()); print(sys.argv[1:])",
            "$(touch nope)",
            "",
        ),
        os.environ,
    )
    assert output == f"{tmp_path}\n['$(touch nope)', '']\n"


def test_interactive_command_inherits_streams(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert (
        run_command(
            tmp_path, (sys.executable, "-c", "print('interactive')"), os.environ, capture=False
        )
        == ""
    )
    assert capfd.readouterr().out == "interactive\n"


@pytest.mark.parametrize("capture", [True, False])
def test_nonzero_command_is_error(tmp_path: Path, capture: bool) -> None:
    with pytest.raises(ReleaseError, match=r"Command failed \(7\)"):
        run_command(
            tmp_path, (sys.executable, "-c", "import sys; sys.exit(7)"), os.environ, capture=capture
        )


def test_failure_includes_stderr(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="diagnostic"):
        run_command(
            tmp_path,
            (sys.executable, "-c", "import sys; sys.stderr.write('diagnostic'); sys.exit(1)"),
            os.environ,
        )


def test_missing_executable_is_error(tmp_path: Path) -> None:
    with pytest.raises(ReleaseError, match="Cannot run"):
        run_command(tmp_path, ("/no/such/executable",), os.environ)


@pytest.mark.parametrize("failure", ["exit", "oserror", "valueerror"])
def test_command_errors_redact_url_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    url = "https://synthetic-user:synthetic-password@example.invalid/repo.git"
    argv = ("git", "ls-remote", "--", url)

    def failed(command: tuple[str, ...], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert command == argv
        if failure == "oserror":
            raise OSError(f"cannot connect to {url}")
        if failure == "valueerror":
            raise ValueError(f"invalid URL: {url}")
        return subprocess.CompletedProcess(command, 7, b"", f"connection failed: {url}".encode())

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ReleaseError) as raised:
        run_command(tmp_path, argv, os.environ)
    message = str(raised.value)
    assert "git ls-remote" in message
    assert "https://[redacted]@example.invalid/repo.git" in message
    assert "synthetic-user" not in message
    assert "synthetic-password" not in message
