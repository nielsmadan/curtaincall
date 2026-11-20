import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from curtaincall.errors import NothingToRelease, ReleaseCancelled, ReleaseError
from curtaincall.git import git_command
from curtaincall.inspection import inspect_repository
from curtaincall.leftovers import Leftovers, describe_leftovers
from curtaincall.models import (
    ReleaseConfig,
    ReleaseRequest,
    RepositorySnapshot,
    Stage,
    TagState,
)
from curtaincall.process import run_command
from curtaincall.release import (
    _report,
    _require_tools,
    _state_changes,
    release,
)
from curtaincall.stages import prepare
from tests.helpers import executable

pytestmark = pytest.mark.integration


def git(root: Path, environment: dict[str, str], *arguments: str) -> str:
    return git_command(root, arguments, environment).strip()


def command(code: str) -> tuple[str, ...]:
    return (sys.executable, "-c", code)


def stage(
    code: str = "from pathlib import Path; Path('tracked.txt').write_text('{version}')",
    *,
    files: tuple[str, ...] = ("tracked.txt",),
    checks: tuple[tuple[str, ...], ...] = (),
) -> Stage:
    return Stage(files, (command(code),), checks, "chore: prepare {version}")


def run_release(  # noqa: PLR0913
    root: Path,
    config: ReleaseConfig,
    environment: dict[str, str],
    *,
    dry_run: bool = False,
    yes: bool = True,
    version: str | None = None,
    prompt: Callable[[str], str] | None = None,
) -> None:
    release(
        root,
        config,
        ReleaseRequest(version=version, dry_run=dry_run, yes=yes),
        environment,
        prompt=prompt,
    )


def hook(root: Path, name: str, code: str) -> None:
    executable(root / ".git/hooks" / name, f"#!{sys.executable}\n{code}\n")


def released(root: Path, environment: dict[str, str], tag: str = "v0.1.0") -> None:
    git(root, environment, "tag", "-a", "-m", "previous", tag)
    git(root, environment, "push", "origin", f"refs/tags/{tag}")
    (root / "tracked.txt").write_text("fix\n")
    git(root, environment, "commit", "-am", "fix: improve")


def remote_refs(root: Path, environment: dict[str, str]) -> str:
    return git(root, environment, "ls-remote", "origin")


START_ID = "a" * 40
MOVED_ID = "b" * 40


def state(
    *,
    head: str = START_ID,
    origin: str = "origin.git",
    remote: tuple[tuple[str, str], ...] = (),
    tags: tuple[TagState, ...] = (),
) -> RepositorySnapshot:
    return RepositorySnapshot(head, origin, remote, tags)


def leftovers(
    *,
    tip: str | None = START_ID,
    new_commits: int | None = None,
    head: str | None = START_ID,
    head_ref: str | None = "refs/heads/main",
    tag: str | None = None,
    uncommitted: frozenset[str] = frozenset(),
) -> Leftovers:
    return Leftovers("main", START_ID, tip, new_commits, head, head_ref, tag, uncommitted)


def raised_by(call: Callable[[], object]) -> BaseException:
    try:
        call()
    except (ReleaseError, ReleaseCancelled, KeyboardInterrupt) as error:
        return error
    pytest.fail("the call did not raise")


FALLBACK = (
    "Inspect the repository for preparation commits, a switched branch, "
    "uncommitted changes or a local tag."
)


def test_tag_only_release(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    before = git(repository, git_environment, "rev-parse", "HEAD")
    run_release(repository, config, git_environment)
    assert git(repository, git_environment, "rev-parse", "v0.1.0^{commit}") == before
    assert git(repository, git_environment, "cat-file", "-t", "v0.1.0") == "tag"
    assert (
        dict(
            line.split("\t")[::-1] for line in remote_refs(repository, git_environment).splitlines()
        )["refs/tags/v0.1.0^{}"]
        == before
    )


def test_two_stage_revision_and_no_change(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    original = git(repository, git_environment, "rev-parse", "HEAD")
    first = stage("from pathlib import Path; Path('tracked.txt').write_text('{revision}')")
    second = stage(
        "from pathlib import Path; Path('next.txt').write_text('{revision}')", files=("next.txt",)
    )
    policy = replace(config, stages=(first, stage("pass"), second))
    run_release(repository, policy, git_environment)
    first_commit = git(repository, git_environment, "rev-parse", "HEAD^")
    assert (repository / "tracked.txt").read_text() == original
    assert (repository / "next.txt").read_text() == first_commit
    assert git(repository, git_environment, "rev-list", "--count", f"{original}..HEAD") == "2"
    assert git(repository, git_environment, "status", "--porcelain") == ""


def test_tag_placeholders(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    code = "from pathlib import Path; Path('tracked.txt').write_text('{tag}|{tag_prefix}')"
    marker = repository.parent / "check-marker"
    check = command(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('{{tag}}|{{tag_prefix}}')"
    )
    policy = replace(
        config,
        tag_prefix="rust-v",
        stages=(Stage(("tracked.txt",), (command(code),), (check,), "release {tag}"),),
    )
    run_release(repository, policy, git_environment)
    assert (repository / "tracked.txt").read_text() == "rust-v0.1.0|rust-v"
    assert marker.read_text() == "rust-v0.1.0|rust-v"
    assert git(repository, git_environment, "log", "-1", "--format=%s") == "release rust-v0.1.0"
    assert git(repository, git_environment, "cat-file", "-t", "rust-v0.1.0") == "tag"


def test_dry_run_invokes_no_commands_or_extra_tools(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    released(repository, git_environment)
    marker = repository.parent / "marker"
    trap = command(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); raise SystemExit(1)"
    )
    policy = replace(
        config,
        tools=("curtaincall-missing-tool",),
        preflight=(trap,),
        suggest=trap,
        stages=(Stage(("tracked.txt",), (trap,), (trap,), "release"),),
    )
    before = inspect_repository(repository, config, git_environment)
    run_release(repository, policy, git_environment, dry_run=True)
    assert inspect_repository(repository, config, git_environment) == before
    assert marker.exists() is False
    assert "Built-in estimate only" in capsys.readouterr().out


def test_dry_run_leaves_git_directory_untouched(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    os.utime(repository / "tracked.txt", (1, 1))
    index = repository / ".git/index"
    before = index.read_bytes()
    run_release(repository, config, git_environment, dry_run=True)
    assert index.read_bytes() == before


@pytest.mark.parametrize("answer", ["", "n", "no"])
def test_cancel_before_preparation(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    answer: str,
) -> None:
    before = inspect_repository(repository, config, git_environment)
    run_release(
        repository,
        replace(config, stages=(stage(),)),
        git_environment,
        yes=False,
        prompt=lambda _: answer,
    )
    assert inspect_repository(repository, config, git_environment) == before


def test_interactive_override_is_previewed_again(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = iter(("v2.0.0", "y"))
    previews = iter(("first release -> v0.1.0", "first release -> v2.0.0"))

    def answer(_: str) -> str:
        output = capsys.readouterr().out
        assert next(previews) in output
        assert "Commits since repository start: 1" in output
        assert "Features:\n  - feat: initial\nBreaking changes: none\n" in output
        return next(answers)

    run_release(repository, config, git_environment, yes=False, prompt=answer)
    assert git(repository, git_environment, "tag", "--list") == "v2.0.0"


def test_checks_run_before_confirmation(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    released(repository, git_environment)
    for message in (
        "feat(cli): add preview",
        "feat!: replace format",
        "chore: update API\n\nBREAKING CHANGE: old API removed",
        "fix: change defaults\n\nBREAKING-CHANGE: defaults changed",
        "",
    ):
        git(
            repository,
            git_environment,
            "commit",
            "--allow-empty",
            "--allow-empty-message",
            "-m",
            message,
        )
    marker = repository.parent / "checked"
    check = command(
        f"from pathlib import Path; Path({str(marker)!r}).write_text('passed'); print('CHECKS FINISHED')"
    )

    def answer(_: str) -> str:
        assert marker.read_text() == "passed"
        output = capfd.readouterr().out
        assert output.split("CHECKS FINISHED\n")[-1].splitlines() == [
            "",
            "Example: v0.1.0 -> v0.2.0",
            "Commits since previous release: 6",
            "Branch: main; 6 commit(s) ahead of origin",
            "Changes: 2 feature(s), 2 fix(es), 3 breaking change(s)",
            "Features:",
            "  - feat!: replace format",
            "  - feat(cli): add preview",
            "Breaking changes:",
            "  - fix: change defaults",
            "  - chore: update API",
            "  - feat!: replace format",
        ]
        return ""

    run_release(
        repository, replace(config, preflight=(check,)), git_environment, yes=False, prompt=answer
    )
    assert git(repository, git_environment, "tag", "--list") == "v0.1.0"


def test_summary_respects_configured_paths(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    released(repository, git_environment)
    (repository / "other.txt").write_text("outside scope")
    git(repository, git_environment, "add", "other.txt")
    git(repository, git_environment, "commit", "-m", "feat!: unrelated feature")
    run_release(
        repository, replace(config, history_paths=("tracked.txt",)), git_environment, dry_run=True
    )
    assert capsys.readouterr().out.splitlines() == [
        "",
        "Example: v0.1.0 -> v0.1.1",
        "Commits since previous release (history paths): 1",
        "Branch: main; 2 commit(s) ahead of origin",
        "Changes: 0 feature(s), 1 fix(es), 0 breaking change(s)",
        "Features: none",
        "Breaking changes: none",
        "Dry run: checks, preparation, tagging and publication skipped.",
    ]


@pytest.mark.parametrize("during_prompt", [False, True])
def test_reviewed_state_changes_stop_release(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    during_prompt: bool,
) -> None:
    def change(_: str) -> str:
        git(repository, git_environment, "commit", "--allow-empty", "-m", "fix: concurrent")
        return "yes"

    policy = (
        config
        if during_prompt
        else replace(config, preflight=(("git", "commit", "--allow-empty", "-m", "fix: check"),))
    )
    head = git(repository, git_environment, "rev-parse", "HEAD")
    with pytest.raises(
        ReleaseError, match=rf"reviewed repository state changed \(HEAD moved from {head[:12]} to "
    ):
        run_release(
            repository,
            policy,
            git_environment,
            yes=not during_prompt,
            prompt=change if during_prompt else None,
        )
    assert git(repository, git_environment, "tag", "--list") == ""


def test_preflight_failure_preserves_state(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    before = inspect_repository(repository, config, git_environment)
    with pytest.raises(ReleaseError, match=r"'raise SystemExit\(7\)' failed \(7\)$"):
        run_release(
            repository,
            replace(config, preflight=(command("raise SystemExit(7)"),)),
            git_environment,
        )
    assert inspect_repository(repository, config, git_environment) == before


def test_missing_tool_fails_before_commands(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    with pytest.raises(ReleaseError, match="Required release tool"):
        run_release(
            repository, replace(config, tools=("curtaincall-nonexistent",)), git_environment
        )
    assert git(repository, git_environment, "tag", "--list") == ""


def test_missing_path_uses_system_default_tools(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    environment = {key: value for key, value in git_environment.items() if key != "PATH"}
    _require_tools(repository, replace(config, tools=("sh",)), environment)
    assert run_command(repository, ("sh", "-c", "printf system-tool-ran"), environment) == (
        "system-tool-ran"
    )


def test_missing_path_rejects_repository_only_tool(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    tool = "curtains-root-only-tool"
    executable = repository / tool
    executable.write_text(f"#!{sys.executable}\nprint('repository-tool-ran')\n")
    executable.chmod(0o755)
    environment = {key: value for key, value in git_environment.items() if key != "PATH"}
    assert run_command(repository, (f"./{tool}",), environment) == "repository-tool-ran\n"
    with pytest.raises(ReleaseError, match=f"Cannot run {tool}:"):
        run_command(repository, (tool,), environment)
    with pytest.raises(ReleaseError, match=f"Required release tool is unavailable: {tool}"):
        _require_tools(repository, replace(config, tools=(tool,)), environment)


def test_custom_suggestion(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    released(repository, git_environment, "release/0.1.0")
    policy = replace(config, tag_prefix="release/", suggest=command("print('release/0.2.0')"))
    run_release(repository, policy, git_environment)
    assert git(repository, git_environment, "tag", "--list").splitlines() == [
        "release/0.1.0",
        "release/0.2.0",
    ]


@pytest.mark.parametrize("output", ["0.1.0", "0.0.1"])
def test_unattended_release_without_bump_releases_nothing(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], output: str
) -> None:
    released(repository, git_environment, "release/0.1.0")
    marker = repository.parent / "checked"
    policy = replace(
        config,
        tag_prefix="release/",
        suggest=command(f"print({output!r})"),
        preflight=(command(f"from pathlib import Path; Path({str(marker)!r}).touch()"),),
    )
    with pytest.raises(
        NothingToRelease,
        match=r"^No automatic release; supply a version override to release these changes\.$",
    ):
        run_release(repository, policy, git_environment)
    assert marker.exists() is False
    assert git(repository, git_environment, "tag", "--list") == "release/0.1.0"


@pytest.mark.parametrize(
    "code, reason",
    [
        ("print('invalid')", "canonical"),
        ("print('0.2.0\\n0.3.0')", "canonical"),
        ("raise SystemExit(3)", r"failed \(3\)"),
        ("pass", "canonical"),
    ],
)
def test_invalid_suggestion_fails(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], code: str, reason: str
) -> None:
    released(repository, git_environment)
    before = inspect_repository(repository, config, git_environment)
    with pytest.raises(ReleaseError, match=reason):
        run_release(repository, replace(config, suggest=command(code)), git_environment)
    assert inspect_repository(repository, config, git_environment) == before


@pytest.mark.parametrize("first_release", [False, True])
def test_suggestion_skipped_with_override_or_initial(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], first_release: bool
) -> None:
    if not first_release:
        released(repository, git_environment)
    policy = replace(config, suggest=command("raise SystemExit(10)"))
    run_release(repository, policy, git_environment, version=None if first_release else "minor")
    assert git(repository, git_environment, "tag", "--list").splitlines()[-1] == (
        "v0.1.0" if first_release else "v0.2.0"
    )


@pytest.mark.parametrize(
    "code",
    [
        "from pathlib import Path; Path('bad.txt').write_text('bad')",
        "from pathlib import Path; Path('tracked.txt').rename('bad.txt')",
        "from pathlib import Path; Path('tracked.txt').unlink()",
        "from pathlib import Path; Path('allowed-near').mkdir(); Path('allowed-near/x').write_text('bad')",
    ],
)
def test_undeclared_changes_stop_preparation(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], code: str
) -> None:
    before = remote_refs(repository, git_environment)
    with pytest.raises(ReleaseError, match="undeclared paths"):
        run_release(
            repository, replace(config, stages=(stage(code, files=("allowed",)),)), git_environment
        )
    assert remote_refs(repository, git_environment) == before
    assert git(repository, git_environment, "tag", "--list") == ""


@pytest.mark.parametrize(
    "code",
    [
        "import subprocess; subprocess.run(['git','commit','--allow-empty','-m','chore: own commit'],check=True)",
        "import subprocess; subprocess.run(['git','switch','-c','other'],check=True)",
    ],
)
def test_stage_cannot_move_head_or_branch(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], code: str
) -> None:
    with pytest.raises(ReleaseError, match="changed HEAD or switched"):
        run_release(repository, replace(config, stages=(stage(code),)), git_environment)
    assert git(repository, git_environment, "tag", "--list") == ""


def test_stage_checks_and_mutations(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    check = command(
        "from pathlib import Path; assert Path('tracked.txt').read_text() == '{version}'; Path('bad.txt').write_text('bad')"
    )
    with pytest.raises(ReleaseError, match="undeclared paths"):
        run_release(repository, replace(config, stages=(stage(checks=(check,)),)), git_environment)
    assert (repository / "tracked.txt").read_text() == "0.1.0"
    assert (repository / "bad.txt").read_text() == "bad"
    assert git(repository, git_environment, "tag", "--list") == ""


def test_failed_stage_check_preserves_files(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    before = git(repository, git_environment, "rev-parse", "HEAD")
    with pytest.raises(
        ReleaseError,
        match=r"failed \(2\)\nCurtains did not push the release\. Branch main is unchanged; "
        r"uncommitted changes: 'tracked\.txt'\.$",
    ):
        run_release(
            repository,
            replace(config, stages=(stage(checks=(command("raise SystemExit(2)"),)),)),
            git_environment,
        )
    assert git(repository, git_environment, "rev-parse", "HEAD") == before
    assert (repository / "tracked.txt").read_text() == "0.1.0"


def test_hook_changed_allowed_content_is_rechecked(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    record = repository.parent / "checks"
    hook(
        repository,
        "pre-commit",
        "from pathlib import Path\nimport subprocess\nPath('tracked.txt').write_text('hooked')\nsubprocess.run(['git', 'add', 'tracked.txt'], check=True)",
    )
    check = command(
        f"from pathlib import Path; p=Path({str(record)!r}); p.write_text((p.read_text() if p.exists() else '') + Path('tracked.txt').read_text() + '\\n')"
    )
    run_release(repository, replace(config, stages=(stage(checks=(check,)),)), git_environment)
    assert record.read_text().splitlines() == ["0.1.0", "hooked"]
    assert git(repository, git_environment, "show", "v0.1.0:tracked.txt") == "hooked"


def test_hook_content_fails_second_check(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    hook(
        repository,
        "pre-commit",
        "from pathlib import Path\nimport subprocess\nPath('tracked.txt').write_text('bad')\nsubprocess.run(['git', 'add', 'tracked.txt'], check=True)",
    )
    before = git(repository, git_environment, "rev-parse", "HEAD")
    check = command(
        "from pathlib import Path; assert Path('tracked.txt').read_text() == '{version}'"
    )
    with pytest.raises(ReleaseError, match=r"' failed \(1\)\nCurtains did not push"):
        run_release(repository, replace(config, stages=(stage(checks=(check,)),)), git_environment)
    assert git(repository, git_environment, "rev-parse", "HEAD") != before
    assert git(repository, git_environment, "show", "HEAD:tracked.txt") == "bad"
    assert git(repository, git_environment, "tag", "--list") == ""


@pytest.mark.parametrize("hook_kind", ["scope", "dirty", "extra_commit", "branch"])
def test_hook_integrity_failures(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], hook_kind: str
) -> None:
    codes = {
        "scope": "Path('bad.txt').write_text('bad'); subprocess.run(['git','add','bad.txt'],check=True)",
        "dirty": "Path('bad.txt').write_text('bad')",
        "extra_commit": "Path('.git/hooks/post-commit').unlink(); subprocess.run(['git','commit','--allow-empty','-m','chore: extra'],check=True)",
        "branch": "subprocess.run(['git','switch','-c','other'],check=True)",
    }
    hook(
        repository,
        "pre-commit" if hook_kind == "scope" else "post-commit",
        "from pathlib import Path\nimport subprocess\n" + codes[hook_kind],
    )
    reason = {
        "scope": "Preparation changed undeclared paths: 'bad.txt'",
        "dirty": "Preparation left a dirty worktree or index",
        "extra_commit": "exactly the captured HEAD as its single parent",
        "branch": "Preparation changed HEAD or switched branches",
    }[hook_kind]
    before = remote_refs(repository, git_environment)
    with pytest.raises(ReleaseError, match=reason):
        run_release(repository, replace(config, stages=(stage(),)), git_environment)
    assert remote_refs(repository, git_environment) == before
    assert git(repository, git_environment, "tag", "--list") == ""


@pytest.mark.parametrize("from_hook", [False, True])
def test_origin_redirect_after_preparation_is_rejected(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], from_hook: bool
) -> None:
    other = repository.parent / "other.git"
    git(
        repository,
        git_environment,
        "clone",
        "--bare",
        str(repository.parent / "remote.git"),
        str(other),
    )
    code = f"import subprocess; subprocess.run(['git','remote','set-url','origin',{str(other)!r}], check=True)"
    if from_hook:
        hook(repository, "post-commit", code)
        preparation = stage()
    else:
        preparation = replace(stage(), commands=(*stage().commands, command(code)))
    original = remote_refs(repository, git_environment)
    with pytest.raises(ReleaseError, match=r"state changed \(origin's URL changed\)"):
        run_release(repository, replace(config, stages=(preparation,)), git_environment)
    assert (
        git(repository, git_environment, "ls-remote", str(repository.parent / "remote.git"))
        == original
    )
    assert git(repository, git_environment, "ls-remote", str(other)) == original
    assert git(repository, git_environment, "tag", "--list") == ""


def test_chained_fetch_push_rewrites_are_rejected(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    remote = repository.parent / "remote.git"
    other = repository.parent / "other.git"
    git(repository, git_environment, "clone", "--bare", str(remote), str(other))
    original = git(remote, git_environment, "show-ref")
    git(repository, git_environment, "remote", "set-url", "origin", "alias:")
    for key, value in (
        ("url.bridge:.insteadOf", "alias:"),
        (f"url.{remote}.insteadOf", "bridge:"),
        (f"url.{other}.pushInsteadOf", "bridge:"),
    ):
        git(repository, git_environment, "config", key, value)
    assert git(repository, git_environment, "remote", "get-url", "origin") == "bridge:"
    assert git(repository, git_environment, "remote", "get-url", "--push", "origin") == "bridge:"
    with pytest.raises(ReleaseError, match="subject to another URL rewrite"):
        run_release(repository, config, git_environment)
    assert git(remote, git_environment, "show-ref") == original
    assert git(other, git_environment, "show-ref") == original
    assert git(repository, git_environment, "tag", "--list") == ""


@pytest.mark.parametrize("from_hook", [False, True])
def test_second_rewrite_after_preparation_is_rejected(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], from_hook: bool
) -> None:
    remote = repository.parent / "remote.git"
    other = repository.parent / "other.git"
    git(repository, git_environment, "clone", "--bare", str(remote), str(other))
    original = git(remote, git_environment, "show-ref")
    git(repository, git_environment, "remote", "set-url", "origin", "alias:")
    git(repository, git_environment, "config", f"url.{remote}.insteadOf", "alias:")
    assert inspect_repository(repository, config, git_environment).snapshot.origin == str(remote)
    code = f"import subprocess; subprocess.run(['git','config',{f'url.{other}.insteadOf'!r},{str(remote)!r}], check=True)"
    if from_hook:
        hook(repository, "post-commit", code)
        preparation = stage()
    else:
        preparation = replace(stage(), commands=(*stage().commands, command(code)))
    with pytest.raises(ReleaseError, match="subject to another URL rewrite"):
        run_release(repository, replace(config, stages=(preparation,)), git_environment)
    assert git(repository, git_environment, "remote", "get-url", "origin") == str(remote)
    assert git(repository, git_environment, "config", f"url.{other}.insteadOf") == str(remote)
    assert git(repository, git_environment, "show", "HEAD:tracked.txt") == "0.1.0"
    assert git(remote, git_environment, "show-ref") == original
    assert git(other, git_environment, "show-ref") == original
    assert git(repository, git_environment, "tag", "--list") == ""


def test_single_url_rewrite_publishes_to_resolved_destination(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    remote = repository.parent / "remote.git"
    git(repository, git_environment, "remote", "set-url", "origin", "alias:")
    git(repository, git_environment, "config", f"url.{remote}.insteadOf", "alias:")
    run_release(repository, config, git_environment)
    assert git(remote, git_environment, "rev-parse", "v0.1.0^{commit}") == git(
        repository, git_environment, "rev-parse", "HEAD"
    )


def test_atomic_rejection_and_nonatomic_control(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    remote = repository.parent / "remote.git"
    marker = repository.parent / "hook-ran"
    update = remote / "hooks/update"
    update.write_text(
        f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\nif sys.argv[1].startswith('refs/tags/'):\n Path({str(marker)!r}).write_text('rejected')\n sys.exit(1)\n"
    )
    update.chmod(0o755)
    before = remote_refs(repository, git_environment)
    before_head = git(repository, git_environment, "rev-parse", "HEAD")
    with pytest.raises(ReleaseError, match="remote outcome may be uncertain"):
        run_release(repository, replace(config, stages=(stage(),)), git_environment)
    prepared = git(repository, git_environment, "rev-parse", "HEAD")
    tag_object = git(repository, git_environment, "rev-parse", "refs/tags/v0.1.0")
    assert prepared != before_head
    assert marker.read_text() == "rejected"
    assert remote_refs(repository, git_environment) == before
    assert git(repository, git_environment, "cat-file", "-t", tag_object) == "tag"
    with pytest.raises(ReleaseError, match=r"git push --no-follow-tags .* failed \(1\)"):
        git(
            repository,
            git_environment,
            "push",
            "--no-follow-tags",
            str(remote),
            f"{prepared}:refs/heads/main",
            f"{tag_object}:refs/tags/v0.1.0",
        )
    assert (
        git(repository, git_environment, "--git-dir", str(remote), "rev-parse", "refs/heads/main")
        == prepared
    )
    assert git(repository, git_environment, "--git-dir", str(remote), "tag", "--list") == ""


def test_push_ignores_follow_tags_and_recursion_config(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    git(repository, git_environment, "tag", "-a", "-m", "unrelated", "other")
    git(repository, git_environment, "config", "push.followTags", "true")
    git(repository, git_environment, "config", "push.recurseSubmodules", "only")
    run_release(repository, replace(config, stages=(stage(),)), git_environment)
    refs = dict(
        line.split("\t")[::-1] for line in remote_refs(repository, git_environment).splitlines()
    )
    assert set(refs) == {"HEAD", "refs/heads/main", "refs/tags/v0.1.0", "refs/tags/v0.1.0^{}"}
    assert refs["refs/heads/main"] == git(repository, git_environment, "rev-parse", "HEAD")


def test_two_part_and_directory_allowlist(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    policy = replace(
        config,
        version_parts=2,
        initial_version=(1, 0),
        stages=(
            stage(
                "from pathlib import Path; Path('allowed/sub').mkdir(parents=True); Path('allowed/sub/a').write_text('{version}')",
                files=("allowed",),
            ),
        ),
    )
    run_release(repository, policy, git_environment)
    assert git(repository, git_environment, "show", "v1.0:allowed/sub/a") == "1.0"


def test_prepare_preserves_literal_filename(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    name = "line\r\n name.txt"
    preparation = stage(
        f"from pathlib import Path; Path({name!r}).write_text('{{version}}')", files=(name,)
    )
    prepare(
        repository,
        replace(config, stages=(preparation,)),
        (0, 1, 0),
        sys.executable,
        git_environment,
    )
    assert git(repository, git_environment, "show", f"HEAD:{name}") == "0.1.0"
    assert os.fsencode(name) in os.fsencode(
        git_command(repository, ("ls-files", "-z"), git_environment)
    )


def test_explicit_destination_and_object_refspecs(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    record = repository.parent / "push-input"
    hook(
        repository,
        "pre-push",
        f"from pathlib import Path\nimport sys\nPath({str(record)!r}).write_text('\\n'.join(sys.argv[1:]) + '\\n' + sys.stdin.read())",
    )
    run_release(repository, replace(config, stages=(stage(),)), git_environment)
    lines = record.read_text().splitlines()
    destination = str(repository.parent / "remote.git")
    assert lines[:2] == [destination, destination]
    head = git(repository, git_environment, "rev-parse", "HEAD")
    tag = git(repository, git_environment, "rev-parse", "refs/tags/v0.1.0")
    assert {tuple(line.split()[:3]) for line in lines[2:]} == {
        (head, head, "refs/heads/main"),
        (tag, tag, "refs/tags/v0.1.0"),
    }


def test_recursive_submodule_push_is_disabled(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    source = repository.parent / "subsource"
    source.mkdir()
    remote = repository.parent / "subremote.git"
    git(source, git_environment, "init", "--initial-branch=main")
    git(source, git_environment, "init", "--bare", "--initial-branch=main", str(remote))
    for key, value in (
        ("user.name", "Tester"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(source, git_environment, "config", key, value)
    (source / "file").write_text("initial")
    git(source, git_environment, "add", "file")
    git(source, git_environment, "commit", "-m", "feat: initial")
    git(source, git_environment, "push", str(remote), "HEAD:refs/heads/main")
    original = git(source, git_environment, "rev-parse", "HEAD")
    git(repository, git_environment, "submodule", "add", str(remote), "module")
    module = repository / "module"
    for key, value in (
        ("user.name", "Tester"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(module, git_environment, "config", key, value)
    (module / "file").write_text("unpublished")
    git(module, git_environment, "commit", "-am", "fix: local module")
    git(repository, git_environment, "add", "module", ".gitmodules")
    git(repository, git_environment, "commit", "-m", "feat: module")
    git(repository, git_environment, "config", "push.recurseSubmodules", "on-demand")
    run_release(repository, config, git_environment)
    assert git(source, git_environment, "--git-dir", str(remote), "rev-parse", "main") == original
    assert git(
        repository,
        git_environment,
        "--git-dir",
        str(repository.parent / "remote.git"),
        "rev-parse",
        "main",
    ) == git(repository, git_environment, "rev-parse", "HEAD")


@pytest.mark.parametrize("during_stage", [False, True])
def test_unrelated_refs_may_change_during_release(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], during_stage: bool
) -> None:
    commands = (
        ("git", "tag", "unrelated"),
        ("git", "push", "origin", "HEAD:refs/heads/other", "refs/tags/unrelated"),
    )
    policy = (
        replace(config, stages=(Stage(("tracked.txt",), commands, (), "unchanged"),))
        if during_stage
        else replace(config, preflight=commands)
    )
    run_release(repository, policy, git_environment)
    assert git(repository, git_environment, "tag", "--list").splitlines() == ["unrelated", "v0.1.0"]


def test_historical_release_refs_revalidated_after_preparation(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    released(repository, git_environment)
    before = git(repository, git_environment, "rev-parse", "HEAD")
    rewrite = command("import subprocess; subprocess.run(['git','tag','-f','v0.1.0'],check=True)")
    policy = replace(config, stages=(replace(stage(), commands=(*stage().commands, rewrite)),))
    with pytest.raises(ReleaseError, match="different local and origin targets"):
        run_release(repository, policy, git_environment)
    assert git(repository, git_environment, "rev-parse", "HEAD") != before
    assert git(repository, git_environment, "tag", "--list") == "v0.1.0"


def test_remote_branch_revalidated_after_preparation(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    initial = git(repository, git_environment, "rev-parse", "HEAD")
    (repository / "tracked.txt").write_text("before release")
    git(repository, git_environment, "commit", "-am", "fix: new change")
    before = git(repository, git_environment, "rev-parse", "HEAD")
    update = command(
        "import subprocess; subprocess.run(['git','push','origin','HEAD:refs/heads/main'],check=True)"
    )
    policy = replace(config, stages=(replace(stage(), commands=(*stage().commands, update)),))
    with pytest.raises(
        ReleaseError, match=rf"origin's main moved from {initial[:12]} to {before[:12]}\)"
    ):
        run_release(repository, policy, git_environment)
    assert (
        git(
            repository,
            git_environment,
            "--git-dir",
            str(repository.parent / "remote.git"),
            "rev-parse",
            "main",
        )
        == before
    )
    assert git(repository, git_environment, "tag", "--list") == ""


def test_unsupported_atomic_push_preserves_artifacts(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    remote = repository.parent / "remote.git"
    git(remote, git_environment, "config", "receive.advertiseAtomic", "false")
    before = remote_refs(repository, git_environment)
    with pytest.raises(ReleaseError, match="Publication failed"):
        run_release(repository, replace(config, stages=(stage(),)), git_environment)
    assert remote_refs(repository, git_environment) == before
    assert git(repository, git_environment, "tag", "--list") == "v0.1.0"
    assert git(repository, git_environment, "show", "HEAD:tracked.txt") == "0.1.0"


def signing_key(root: Path, environment: dict[str, str]) -> None:
    key = root.parent / "signing-key"
    subprocess.run(
        ("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)),
        env=environment,
        check=True,
        capture_output=True,
    )
    signers = root.parent / "allowed-signers"
    signers.write_text(f"release@example.invalid {key.with_suffix('.pub').read_text()}")
    for setting, value in (
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
        ("gpg.ssh.allowedSignersFile", str(signers)),
    ):
        git(root, environment, "config", setting, value)


def test_sign_requires_signed_commits_and_tag(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    signing_key(repository, git_environment)
    run_release(repository, replace(config, stages=(stage(),), sign=True), git_environment)
    assert "Signing: required for preparation commits and the release tag" in (
        capsys.readouterr().out.splitlines()
    )
    git(repository, git_environment, "verify-commit", "HEAD")
    git(repository, git_environment, "verify-tag", "v0.1.0")
    assert "refs/tags/v0.1.0" in remote_refs(repository, git_environment)


def test_unset_sign_follows_git_tag_config(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    signing_key(repository, git_environment)
    git(repository, git_environment, "config", "tag.gpgsign", "true")
    run_release(repository, config, git_environment)
    git(repository, git_environment, "verify-tag", "v0.1.0")


def test_sign_without_key_fails_before_commands(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    git(repository, git_environment, "config", "gpg.format", "ssh")
    head = git(repository, git_environment, "rev-parse", "HEAD")
    before = remote_refs(repository, git_environment)
    marker = repository.parent / "checked"
    policy = replace(
        config,
        preflight=(command(f"from pathlib import Path; Path({str(marker)!r}).touch()"),),
        stages=(stage(),),
        sign=True,
    )
    with pytest.raises(ReleaseError, match=r"(?s)Git cannot sign: .*user\.signingkey"):
        run_release(repository, policy, git_environment)
    assert not marker.exists()
    assert git(repository, git_environment, "rev-parse", "HEAD") == head
    assert git(repository, git_environment, "status", "--porcelain") == ""
    assert remote_refs(repository, git_environment) == before
    assert git(repository, git_environment, "tag", "--list") == ""


def test_override_requires_relevant_history(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    git(repository, git_environment, "tag", "v0.1.0")
    git(repository, git_environment, "push", "origin", "refs/tags/v0.1.0")
    with pytest.raises(ReleaseError, match="no relevant commits"):
        run_release(repository, config, git_environment, version="2.0.0")
    assert git(repository, git_environment, "tag", "--list") == "v0.1.0"


def test_invalid_interactive_answer_asks_again(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers = iter(("ys", "1.3", "v0.2.0", "y"))
    run_release(repository, config, git_environment, yes=False, prompt=lambda _: next(answers))
    output = capsys.readouterr().out
    assert "Expected a canonical 3-part version, got 'ys'." in output
    assert "Expected a canonical 3-part version, got '1.3'." in output
    assert git(repository, git_environment, "tag", "--list") == "v0.2.0"


def test_missing_terminal_fails_before_preflight(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    marker = repository.parent / "checked"
    policy = replace(
        config,
        preflight=(command(f"from pathlib import Path; Path({str(marker)!r}).touch()"),),
        suggest=command(f"from pathlib import Path; Path({str(marker)!r}).touch()"),
    )
    with pytest.raises(ReleaseError, match="Interactive confirmation requires a terminal"):
        run_release(repository, policy, git_environment, yes=False)
    assert marker.exists() is False


@pytest.mark.parametrize("keyword", ["patch", "minor", "major"])
def test_bump_keyword_on_first_release_uses_initial_version(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], keyword: str
) -> None:
    run_release(repository, config, git_environment, version=keyword)
    assert git(repository, git_environment, "tag", "--list") == "v0.1.0"


def test_no_automatic_bump_can_be_overridden_interactively(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released(repository, git_environment)
    policy = replace(config, suggest=command("print('0.1.0')"))
    answers = iter(("minor", "yes"))
    run_release(repository, policy, git_environment, yes=False, prompt=lambda _: next(answers))
    assert git(repository, git_environment, "tag", "--list").splitlines() == ["v0.1.0", "v0.2.0"]


def test_report_encodes_links_and_explains_async_draft(
    config: ReleaseConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    policy = replace(config, repository="owner/project", workflow="release.yml", draft=True)
    _report(policy, "release/0.1.0", "1234")
    assert capsys.readouterr().out.splitlines() == [
        "Pushed release/0.1.0 at 1234.",
        "Version tag pushed.",
        "Tag: https://github.com/owner/project/releases/tag/release%2F0.1.0",
        "Publication runs asynchronously: https://github.com/owner/project/actions/workflows/release.yml?query=branch%3Arelease%2F0.1.0",
        "Review and publish the draft when the workflow finishes: https://github.com/owner/project/releases",
    ]


def test_hook_cannot_hide_undeclared_gitlink_change(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    original = git(repository, git_environment, "rev-parse", "HEAD")
    (repository / "module").mkdir()
    git(
        repository,
        git_environment,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{original},module",
    )
    git(repository, git_environment, "commit", "-m", "chore: add gitlink")
    next_target = git(repository, git_environment, "rev-parse", "HEAD")
    git(repository, git_environment, "config", "diff.ignoreSubmodules", "all")
    hook(
        repository,
        "pre-commit",
        f"import subprocess\nsubprocess.run(['git','update-index','--cacheinfo','160000,{next_target},module'],check=True)",
    )
    with pytest.raises(ReleaseError, match="undeclared paths: 'module'"):
        run_release(repository, replace(config, stages=(stage(),)), git_environment)
    assert git(repository, git_environment, "tag", "--list") == ""


def test_stage_failure_reports_what_preparation_left(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    before = git(repository, git_environment, "rev-parse", "HEAD")
    failing = Stage(
        ("notes.txt",),
        (
            command(
                "from pathlib import Path; Path('notes.txt').write_text('x'); raise SystemExit(4)"
            ),
        ),
        (),
        "chore: notes",
    )
    with pytest.raises(ReleaseError) as raised:
        run_release(repository, replace(config, stages=(stage(), failing)), git_environment)
    after = git(repository, git_environment, "rev-parse", "HEAD")
    assert str(raised.value).splitlines()[-1] == (
        "Curtains did not push the release. "
        f"Branch main moved from {before[:12]} to {after[:12]} (1 new commit); "
        "uncommitted changes: 'notes.txt'."
    )


def test_untouched_failure_reports_clean_state(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    failing = replace(stage(), commands=(command("raise SystemExit(4)"),))
    with pytest.raises(ReleaseError) as raised:
        run_release(repository, replace(config, stages=(failing,)), git_environment)
    assert str(raised.value).splitlines()[-1] == (
        "Curtains did not push the release. Branch main is unchanged; the worktree is clean."
    )


@pytest.mark.parametrize("commit_after", [False, True])
def test_stage_detaching_head_is_reported(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str], commit_after: bool
) -> None:
    script = "git checkout -q --detach" + (
        " && git commit -q --allow-empty -m detached" if commit_after else ""
    )
    detach = replace(stage(), commands=(("sh", "-c", script),))
    with pytest.raises(ReleaseError) as raised:
        run_release(repository, replace(config, stages=(detach,)), git_environment)
    head = git(repository, git_environment, "rev-parse", "HEAD")
    assert str(raised.value).splitlines() == [
        "Preparation changed HEAD or switched branches; inspect local artifacts.",
        (
            "Curtains did not push the release. Branch main is unchanged; "
            f"HEAD is detached at {head[:12]}; the worktree is clean."
        ),
    ]


def test_stage_deleting_release_branch_is_reported(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    delete = replace(
        stage(), commands=(("sh", "-c", "git checkout -q --detach && git branch -q -D main"),)
    )
    with pytest.raises(ReleaseError) as raised:
        run_release(repository, replace(config, stages=(delete,)), git_environment)
    head = git(repository, git_environment, "rev-parse", "HEAD")
    assert str(raised.value).splitlines() == [
        "Preparation changed HEAD or switched branches; inspect local artifacts.",
        (
            "Curtains did not push the release. Branch main was deleted; "
            f"HEAD is detached at {head[:12]}; the worktree is clean."
        ),
    ]


def test_stage_rewriting_history_reports_move_without_count(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    before = git(repository, git_environment, "rev-parse", "HEAD")
    rewrite = command(
        "import subprocess; tree = subprocess.run(['git', 'write-tree'], "
        "capture_output=True, text=True, check=True).stdout.strip(); "
        "root = subprocess.run(['git', 'commit-tree', tree, '-m', 'root'], "
        "capture_output=True, text=True, check=True).stdout.strip(); "
        "subprocess.run(['git', 'reset', '--soft', root], check=True)"
    )
    with pytest.raises(ReleaseError) as raised:
        run_release(
            repository,
            replace(config, stages=(replace(stage(), commands=(rewrite,)),)),
            git_environment,
        )
    after = git(repository, git_environment, "rev-parse", "HEAD")
    assert str(raised.value).splitlines()[-1] == (
        "Curtains did not push the release. "
        f"Branch main moved from {before[:12]} to {after[:12]}; the worktree is clean."
    )


def test_tag_left_by_failed_preparation_is_reported(
    repository: Path, config: ReleaseConfig, git_environment: dict[str, str]
) -> None:
    sneaky = stage(checks=(("git", "tag", "-a", "-m", "early", "v0.1.0"),))
    with pytest.raises(ReleaseError, match=r"only local: v0\.1\.0") as raised:
        run_release(repository, replace(config, stages=(sneaky,)), git_environment)
    assert (
        str(raised.value)
        .splitlines()[-1]
        .endswith("(1 new commit); local tag v0.1.0 was created; the worktree is clean.")
    )


@pytest.mark.parametrize("left", ["nothing", "commit", "file"])
def test_interrupted_preparation_reports_leftovers(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    left: str,
) -> None:
    before = git(repository, git_environment, "rev-parse", "HEAD")

    def interrupted(*_: object) -> None:
        if left == "commit":
            git(repository, git_environment, "commit", "--allow-empty", "-m", "chore: prepare")
        if left == "file":
            (repository / "tracked.txt").write_text("partial")
        raise KeyboardInterrupt

    monkeypatch.setattr("curtaincall.release.prepare", interrupted)
    cancelled = raised_by(lambda: run_release(repository, config, git_environment))
    after = git(repository, git_environment, "rev-parse", "HEAD")
    expected = {
        "nothing": "Branch main is unchanged; the worktree is clean.",
        "commit": f"Branch main moved from {before[:12]} to {after[:12]} (1 new commit); "
        "the worktree is clean.",
        "file": "Branch main is unchanged; uncommitted changes: 'tracked.txt'.",
    }[left]
    assert isinstance(cancelled, ReleaseCancelled)
    assert str(cancelled) == f"Release cancelled before the push. {expected}"
    assert cancelled.left_changes is (left != "nothing")


def interrupt_inspection(monkeypatch: pytest.MonkeyPatch, first: BaseException) -> None:
    def interrupted(*_: object) -> None:
        raise first

    def interrupted_again(*_: object) -> frozenset[str]:
        raise KeyboardInterrupt

    monkeypatch.setattr("curtaincall.release.prepare", interrupted)
    monkeypatch.setattr("curtaincall.leftovers.changed_paths", interrupted_again)


def test_second_interrupt_during_cancellation_keeps_cancellation(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt_inspection(monkeypatch, KeyboardInterrupt())
    cancelled = raised_by(lambda: run_release(repository, config, git_environment))
    assert isinstance(cancelled, ReleaseCancelled)
    assert str(cancelled) == f"Release cancelled before the push. {FALLBACK}"
    assert cancelled.left_changes is True


def test_interrupt_during_failure_inspection_keeps_failure(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt_inspection(monkeypatch, ReleaseError("stage failed"))
    failed = raised_by(lambda: run_release(repository, config, git_environment))
    assert isinstance(failed, ReleaseError)
    assert str(failed) == f"stage failed\nCurtains did not push the release. {FALLBACK}"


def test_leftover_inspection_failure_keeps_original_error(
    repository: Path,
    config: ReleaseConfig,
    git_environment: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*_: object) -> frozenset[str]:
        raise ReleaseError("inspection broke")

    monkeypatch.setattr("curtaincall.leftovers.changed_paths", broken)
    failing = replace(stage(), commands=(command("raise SystemExit(4)"),))
    with pytest.raises(ReleaseError) as raised:
        run_release(repository, replace(config, stages=(failing,)), git_environment)
    assert str(raised.value).splitlines()[0].endswith("'raise SystemExit(4)' failed (4)")
    assert str(raised.value).splitlines()[-1] == f"Curtains did not push the release. {FALLBACK}"


@pytest.mark.parametrize(
    "found, expected",
    [
        (leftovers(), "Branch main is unchanged; the worktree is clean."),
        (
            leftovers(
                tip=MOVED_ID,
                new_commits=2,
                head=MOVED_ID,
                head_ref="refs/heads/other",
                tag="v1.0.0",
                uncommitted=frozenset({"b.txt", "a.txt"}),
            ),
            (
                f"Branch main moved from {START_ID[:12]} to {MOVED_ID[:12]} (2 new commits); "
                f"HEAD is on branch 'other' at {MOVED_ID[:12]}; local tag v1.0.0 was created; "
                "uncommitted changes: 'a.txt', 'b.txt'."
            ),
        ),
        (
            leftovers(tip=None, head=MOVED_ID, head_ref=None),
            f"Branch main was deleted; HEAD is detached at {MOVED_ID[:12]}; the worktree is clean.",
        ),
        (
            leftovers(head=None, head_ref="refs/heads/x\u009b2J\u202eevil"),
            (
                "Branch main is unchanged; HEAD is on branch 'x\\x9b2J\\u202eevil'; "
                "the worktree is clean."
            ),
        ),
        (
            leftovers(tip=MOVED_ID, uncommitted=frozenset({f"{index}.txt" for index in range(7)})),
            (
                f"Branch main moved from {START_ID[:12]} to {MOVED_ID[:12]}; uncommitted changes: "
                "'0.txt', '1.txt', '2.txt', '3.txt', '4.txt' and 2 more."
            ),
        ),
        (
            leftovers(uncommitted=frozenset({"new\nline\x1b[2J"})),
            "Branch main is unchanged; uncommitted changes: 'new\\nline\\x1b[2J'.",
        ),
        (None, FALLBACK),
    ],
)
def testdescribe_leftovers(found: Leftovers | None, expected: str) -> None:
    assert describe_leftovers(found) == expected


@pytest.mark.parametrize(
    "found, changed",
    [
        (leftovers(), False),
        (leftovers(tip=MOVED_ID), True),
        (leftovers(head_ref=None), True),
        (leftovers(head_ref="refs/heads/other"), True),
        (leftovers(tag="v1.0.0"), True),
        (leftovers(uncommitted=frozenset({"a.txt"})), True),
    ],
)
def test_leftovers_changed(found: Leftovers, changed: bool) -> None:
    assert found.changed is changed


def test_state_changes_name_each_difference() -> None:
    reviewed = state(
        remote=(
            ("refs/heads/main", START_ID),
            ("refs/tags/v1.0.0", START_ID),
            ("refs/tags/v1.0.0^{}", START_ID),
        ),
        tags=(TagState("v1.0.0", START_ID, START_ID, (1, 0, 0)),),
    )
    current = state(
        head=MOVED_ID,
        origin="elsewhere.git",
        remote=(("refs/heads/main", MOVED_ID), ("refs/tags/v1.1.0", MOVED_ID)),
        tags=(TagState("v1.0.0", MOVED_ID, MOVED_ID, (1, 0, 0)),),
    )
    assert _state_changes(reviewed, current, START_ID) == [
        f"HEAD moved from {START_ID[:12]} to {MOVED_ID[:12]}",
        "origin's URL changed",
        f"origin's tag v1.1.0 appeared at {MOVED_ID[:12]}",
        f"origin's tag v1.0.0 was deleted (was {START_ID[:12]})",
        f"origin's main moved from {START_ID[:12]} to {MOVED_ID[:12]}",
        f"local tag v1.0.0 moved from {START_ID[:12]} to {MOVED_ID[:12]}",
    ]
