# Releases and recovery

## Before running

Use Git 2.45+ and a server supporting atomic pushes. The worktree must be clean, including index and untracked files, on the configured branch with a born, attached HEAD. Shallow history is rejected. Origin must have exactly one effective fetch and push destination, and they must match. A single-step URL rewrite is allowed; a resolved destination still subject to another rewrite is rejected.

Inspection reads `ls-remote` without fetching. The remote release branch must already exist locally as an object and be an ancestor of HEAD. Missing objects fail, including in partial clones with lazy fetching disabled. Fetch or repair deliberately outside the release command, then restart.

Every canonical release tag in the configured prefix must exist both locally and remotely and peel to the same commit, including old tags. The newest version must be an ancestor of HEAD. Tags with other component counts, unrelated tags and prerelease-shaped tags are ignored. There must be relevant commits since the latest release, even for an explicit override. Scoped history affects proposals, while the displayed ahead count includes all branch commits.

Two uncommon inspection limitations remain: a relevant commit range containing only empty messages is rejected, even with a version override; ref names containing Unicode line separators can cause inspection to fail.

## Invocation

`curtains [patch|minor|major|VERSION] [--config PATH] [--directory DIR] [--dry-run] [--yes]`.

Bare invocation previews a proposal, runs checks, then asks for confirmation. Enter or `n` cancels; entering a version previews it again before confirmation. Non-terminal use requires `--yes`. If no automatic bump exists, `--yes` returns without releasing; provide an override. `--help` and `--version` work outside Git repositories.

Dry-run uses read-only inspection and contacts origin. It skips tool requirements and every configured command, including preflight checks, preparation and custom suggestions. A skipped custom suggestion produces a labelled built-in estimate; it is not the custom tool's result.

Checks run before confirmation and are trusted commands: they may leave artifacts even if you cancel afterward. A cancellation, EOF or interruption before publication returns 0; failures return 1; argument usage errors return 2. An interruption during push is a publication failure with uncertain outcome.

## Failure handling

Engine-generated command errors redact URL userinfo, including credentials embedded in an origin URL, while retaining the operation and host. Git receives the original destination. Configured commands and hooks still control their own terminal output.

The engine preserves local work: prepared files, commits and any created annotated tag remain available for inspection. It does not repair, reset, retry, resume or delete/recreate public tags. If a stage fails, examine the worktree, index, commit parent/scope, hook output and stage checks before deciding how to recover. A rejection after preparation can leave a clean tree with unpublished preparation commits.

If publication fails or is interrupted, the remote outcome may be unknown. Inspect the remote branch and tag (including the tag's peeled target), compare them with the prepared local commit and tag object, and decide explicitly what to do. Do not assume the remote is unchanged or rerun blindly. Unsupported atomic push is an error; the engine never falls back.

With `repository`/`workflow` configured, success prints GitHub tag/workflow links and says publication runs asynchronously. `draft` adds a review link. Curtaincall does not invoke `gh`, create releases, wait for Actions, sign artifacts, upload packages or publish drafts. The consumer's existing tag-triggered workflow owns those steps.

## Releasing Curtaincall itself

[The self-policy](../scripts/release.json) uses `main`, prefix `v`, and initial version `1.0.0`. Run from this checkout:

```sh
just release --dry-run
just release             # first release: 1.0.0; later releases use commit messages
# equivalent source command:
uv run --locked curtains
```

This uses the locked source environment, independent of a global `curtains` installation. [prepare_release.py](../scripts/prepare_release.py) validates the requested three-part version and exactly one manifest version in `[project]` before writing. The stage then runs `uv lock` and `just changelog VERSION`, updating only `pyproject.toml`, `uv.lock` and `CHANGELOG.md`. Preflight runs `just check-static` and `just coverage` on HEAD; the stage edits only those three files, and `just build` plus `just check-package` verify their effect on the package before the engine commits it. `just release` disables Lefthook for the engine's commits and push, since preflight already ran the same gates. The tag workflow reruns every gate on the tagged source before publishing.

Changelog generation pins `git-cliff@2.13.1` and uses [cliff.toml](../cliff.toml). Its uv tool/cache/Python directories live under `.uv`; the first invocation may download prerequisites. `just changelog VERSION` regenerates the file without creating a tag. The initial changelog records that no versions have been released.

Package version comes from installed metadata, with `pyproject.toml` as the source manifest; there is no runtime version constant. Commit the setup changes and push `main` before the first release, then run the commands above from a clean checkout. Later releases can use an explicit bump, such as `just release minor`.

### GitHub publication

[release.yml](../.github/workflows/release.yml) runs when a `v*` tag is pushed. It requires a canonical three-part annotated tag whose commit is on `origin/main`, checks that the package version matches, and builds from that exact commit. It runs static analysis, tests with branch coverage, isolated package acceptance, and installation smoke tests of the actual wheel and sdist. The wheel is built from the sdist.

The verification job has read-only permissions. A separate publishing job rechecks the tag and publishes a GitHub release containing the wheel, source archive and `SHA256SUMS`, with notes generated by the pinned git-cliff configuration. It uses GitHub's automatic `GITHUB_TOKEN`; no additional release secret is required. Tags for the same version are serialized by workflow concurrency. PyPI and Homebrew publication are not configured.

After a release exists, install it directly from its asset URL with `uv tool install --reinstall https://github.com/nielsmadan/curtaincall/releases/download/vVERSION/curtaincall-VERSION-py3-none-any.whl`, or download the wheel from [GitHub Releases](https://github.com/nielsmadan/curtaincall/releases) and run `uv tool install --reinstall /path/to/curtaincall-VERSION-py3-none-any.whl`. To verify a download, fetch `SHA256SUMS` into the same directory and run `shasum -a 256 -c --ignore-missing SHA256SUMS` (macOS) or `sha256sum -c --ignore-missing SHA256SUMS` (Linux); `--ignore-missing` skips assets you did not download. The checkout's `just install` remains available.

### Publication recovery

If Actions fails after the tag was pushed, fix the workflow on `main` if needed and use **Run workflow** on the Release workflow, selecting branch `main` and the existing tag. The CLI equivalent is:

```sh
gh workflow run release.yml --ref main -f tag=v1.0.0
```

Manual dispatch uses the workflow and publication helper from `main` while rebuilding the original tagged source. It never retags a newer commit. [release_workflow.py](../scripts/release_workflow.py) accepts an identical existing release, uploads only missing assets, and finishes a matching draft. It compares existing asset sizes and SHA-256 digests and refuses divergent notes, assets, prerelease state or moved tags. An immutable release missing assets also fails. Inspect the failed run and existing release before repairing anything; rerunning `just release` is not a publication retry.
