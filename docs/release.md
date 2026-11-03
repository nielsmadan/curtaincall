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

Checks run before confirmation and are trusted commands: they may leave artifacts even if you cancel afterward. A cancellation, EOF or interruption before the push returns 0; failures return 1; argument usage errors return 2. An interruption during the push is a push failure with uncertain outcome.

## Failure handling

Engine-generated command errors redact URL userinfo, including credentials embedded in an origin URL, while retaining the operation and host. Git receives the original destination. Configured commands and hooks still control their own terminal output.

The engine preserves local work: prepared files, commits and any created annotated tag remain available for inspection. It does not repair, reset, retry, resume or delete/recreate public tags. If a stage fails, examine the worktree, index, commit parent/scope, hook output and stage checks before deciding how to recover. A rejection after preparation can leave a clean tree with unpublished preparation commits.

If the push fails or is interrupted, the remote outcome may be unknown. Inspect the remote branch and tag (including the tag's peeled target), compare them with the prepared local commit and tag object, and decide explicitly what to do. Do not assume the remote is unchanged or rerun blindly. Unsupported atomic push is an error; the engine never falls back.

With `repository`/`workflow` configured, success prints GitHub tag/workflow links and says publication runs asynchronously. `draft` adds a review link. The release command itself does not invoke `gh`, create releases, wait for Actions, sign artifacts, upload packages or publish drafts; the consumer's tag-triggered workflow owns those steps. Inside that workflow, [`curtains ci`](#ci-commands) validates the tag and creates and verifies the GitHub release.

## CI commands

`curtains ci` runs inside a tag-triggered workflow, after the release command pushed the tag. It reads the same policy as the release command (`--config`, `--directory`, given after the subcommand). In GitHub Actions, the [action](migration.md#replacing-a-ci-release-helper) installs `curtains`.

| Command | Behavior |
| --- | --- |
| `curtains ci validate-tag TAG [EVENT]` | Prints the commit of TAG. EVENT, if given, is a full commit SHA or `refs/tags/TAG` and must identify the same commit (`GITHUB_SHA` on a tag push). |
| `curtains ci previous-tag TAG` | Prints the highest release tag below TAG in the policy's prefix and component count, or nothing when there is none. |
| `curtains ci publish TAG TARGET --notes FILE [--title TITLE] [--prerelease] [--not-latest] [ASSET...]` | Creates or completes the GitHub release for TAG; prints one line per action. |

`validate-tag` requires TAG to be a canonical release tag of the policy, present locally, annotated, and contained in `refs/remotes/origin/<branch>`. When the policy has `repository`, the origin must match it. `previous-tag` reads local tags. Both reject a shallow clone, so check out with `fetch-depth: 0` and make sure the branch and tags are fetched (`actions/checkout` then fetches all branches and tags). Errors print `curtains: MESSAGE` on stderr; URL userinfo in quoted `git` or `gh` output is redacted. Exit statuses: 0 success, 1 failure, 2 usage error, 130 interrupted.

`publish` needs `repository` in the policy, the `gh` CLI and a `GH_TOKEN` with `contents: write`. Before any write it checks that the origin matches `repository`, TARGET is a full 40- or 64-digit lowercase hex commit, the notes and assets are readable, asset paths contain no `#`, `*`, `?` or `[`, asset names start with a letter or digit, contain only letters, digits, `.`, `_`, `+` and `-`, have no `..` and do not end in `.` (names GitHub would rename on upload), and are unique, and `git ls-remote origin` shows TAG as an annotated tag peeling to TARGET. That `ls-remote` needs read access to origin, so a private repository needs credentials in the checkout. The default title is `NAME VERSION` (policy `name`, version without the prefix). The notes file becomes the release body, and a retry must pass identical notes. Relative notes and asset paths resolve against `--directory` (default: the working directory), like `--config`.

The sequence is: create a draft release (`--verify-tag`), upload the assets, check the remote tag again and that the draft holds every expected asset, then publish with `--draft=false` (plus `--prerelease` and `--latest=false` when requested). `gh` runs non-interactively.

A retry views the release first and only completes what is missing. Title, body, tag and prerelease state must equal the expected values. Uploaded assets must match size and SHA-256 digest; any divergence, unexpected asset or duplicate name stops the run without writing, so a retry depends on a reproducible build. On a draft, assets that never finished uploading are deleted and uploaded again, missing assets are uploaded, and the draft is published. A complete published release prints `Release TAG already complete` and changes nothing. An immutable release missing assets is refused. After any change, the run ends by viewing the release again and failing if it is not complete.

With `"draft": true` in the policy, the release stays a draft for manual review and the run prints `Draft TAG ready for review`; a published release under such a policy is refused.

If an upload to an already-published release failed part-way, later runs refuse with "incomplete assets" rather than replacing published files. Delete the named asset, then rerun the workflow:

```sh
gh release delete-asset --repo OWNER/NAME --yes -- TAG ASSET_NAME
```

## Releasing Curtaincall itself

[The self-policy](../scripts/release.json) uses `main`, prefix `v`, and initial version `1.0.0`. Run from this checkout:

```sh
just release --dry-run
just release             # first release: 1.0.0; later releases use commit messages
# equivalent source command:
LEFTHOOK=0 uv run --locked curtains
```

This uses the locked source environment, independent of a global `curtains` installation. [prepare_release.py](../scripts/prepare_release.py) validates the requested three-part version and exactly one manifest version in `[project]` before writing. The stage then runs `uv lock` and `just changelog VERSION`, updating only `pyproject.toml`, `uv.lock` and `CHANGELOG.md`. Preflight runs `just check-static` and `just coverage` on HEAD; the stage edits only those three files, and `just build` plus `just check-package` verify their effect on the package before the engine commits it. `just release` disables Lefthook for the engine's commits and push, since preflight already ran the same gates. The tag workflow reruns every gate on the tagged source before publishing.

Changelog generation pins `git-cliff@2.13.1` and uses [cliff.toml](../cliff.toml). Its uv tool/cache/Python directories live under `.uv`; the first invocation may download prerequisites. `just changelog VERSION` regenerates the file without creating a tag. The initial changelog records that no versions have been released.

Package version comes from installed metadata, with `pyproject.toml` as the source manifest; there is no runtime version constant. Commit the setup changes and push `main` before the first release, then run the commands above from a clean checkout. Later releases can use an explicit bump, such as `just release minor`.

### GitHub publication

[release.yml](../.github/workflows/release.yml) runs when a `v*` tag is pushed. It requires a canonical three-part annotated tag whose commit is on `origin/main`, checks that the package version matches, and builds from that exact commit. It runs static analysis, tests with branch coverage, isolated package acceptance, and installation smoke tests of the actual wheel and sdist. The wheel is built from the sdist.

The verification job has read-only permissions. A macOS job then renders the Homebrew formula from [curtaincall.rb.in](../scripts/curtaincall.rb.in), pinned to the SHA-256 of GitHub's archive for the tag, installs it from source through a temporary tap and runs `brew test`, which checks `curtains --version` and a dry-run against a local bare remote. A separate publishing job rechecks the tag with `curtains ci validate-tag` and publishes with `curtains ci publish` a GitHub release containing the wheel, source archive and `SHA256SUMS`, with notes generated by the pinned git-cliff configuration, using GitHub's automatic `GITHUB_TOKEN`. Only after that does a final job copy the tested formula to `Formula/curtaincall.rb` in [nielsmadan/homebrew-tap](https://github.com/nielsmadan/homebrew-tap), committing only when it changed. Tags for the same version are serialized by workflow concurrency. PyPI publication is not configured.

The tap update needs the `HOMEBREW_TAP_TOKEN` Actions secret: a fine-grained personal access token limited to `nielsmadan/homebrew-tap` with **Contents: Read and write**. The macOS job fails before anything is published when the secret is missing. Rotate it in the repository's Actions secrets when it expires.

After a release exists, `brew install nielsmadan/tap/curtaincall` installs it. Without Homebrew, install it directly from its asset URL with `uv tool install --reinstall https://github.com/nielsmadan/curtaincall/releases/download/vVERSION/curtaincall-VERSION-py3-none-any.whl`, or download the wheel from [GitHub Releases](https://github.com/nielsmadan/curtaincall/releases) and run `uv tool install --reinstall /path/to/curtaincall-VERSION-py3-none-any.whl`. To verify a download, fetch `SHA256SUMS` into the same directory and run `shasum -a 256 -c --ignore-missing SHA256SUMS` (macOS) or `sha256sum -c --ignore-missing SHA256SUMS` (Linux); `--ignore-missing` skips assets you did not download. The checkout's `just install` remains available.

### Publication recovery

If Actions fails after the tag was pushed, fix the workflow on `main` if needed and use **Run workflow** on the Release workflow, selecting branch `main` and the existing tag. The CLI equivalent is:

```sh
gh workflow run release.yml --ref main -f tag=v1.0.0
```

Manual dispatch uses the workflow, `curtains ci` (installed by the action from the same checkout) and formula template from `main` while rebuilding the original tagged source, so a formula fix ships without a new version. It never retags a newer commit. `curtains ci publish` accepts an identical existing release, uploads only missing assets, and finishes a matching draft, as described under [CI commands](#ci-commands). It compares existing asset sizes and SHA-256 digests and refuses divergent title, notes, assets, prerelease state or moved tags. [release_workflow.py](../scripts/release_workflow.py) provides the manifest version check and formula rendering. A retry therefore depends on the rebuild being byte-identical: hatchling's wheel and sdist from the same commit are reproducible, so keep build-time inputs free of timestamps and host paths. An immutable release missing assets also fails, and so does a published release with an incomplete asset (see the manual deletion above). A retry after a successful publication but failed tap update re-tests the formula, accepts the existing release, and updates the tap. Inspect the failed run and existing release before repairing anything; rerunning `just release` is not a publication retry.
