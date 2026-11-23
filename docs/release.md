# Releases and recovery

## Before running

Use Git 2.45+ and a server supporting atomic pushes. The worktree must be clean, including index and untracked files, on the configured branch with a born, attached HEAD. Shallow history is rejected. Origin must have exactly one effective fetch and push destination, and they must match. A single-step URL rewrite is allowed; a resolved destination still subject to another rewrite is rejected.

Inspection reads `ls-remote` without fetching. The remote release branch must already exist locally as an object and be an ancestor of HEAD. Missing objects fail, including in partial clones with lazy fetching disabled. Fetch or repair deliberately outside the release command, then restart.

Every canonical release tag in the configured prefix must exist both locally and remotely and peel to the same commit, including old tags. The newest version must be an ancestor of HEAD. Tags with another number of version parts, unrelated tags and prerelease-shaped tags are ignored. There must be relevant commits since the latest release, or a pending [pin update](#pins-in-a-release), even for an explicit override. Scoped history affects proposals, while the displayed ahead count includes all branch commits.

Two uncommon inspection limitations remain: a relevant commit range containing only empty messages is rejected, even with a version override; ref names containing Unicode line separators can cause inspection to fail.

## Invocation

`curtains [patch|minor|major|VERSION] [--config PATH] [--directory DIR] [--dry-run] [--yes] [--keep-pins]`.

Bare invocation previews a proposal, runs preflight commands, then asks for confirmation. Enter or `n` cancels; entering a version or bump keyword previews it again before confirmation, and an entry that is not a valid version prints why and asks again. Non-terminal use requires `--yes`; without it a real run stops before any configured command runs. If no automatic bump exists, `--yes` prints `No automatic release; supply a version override to release these changes.` and exits 3 without running preflight commands; provide an override. `--version` prints the curtains version, never a release version. `--help` and `--version` work outside Git repositories.

The summary shows the previous and proposed release tags, the number of commits since the previous release (or repository start), a `Pins:` line when the policy names pins, change counts (fixes include pending pin updates, counted separately in brackets), a signing requirement when `sign` is enabled, and lists of feature and breaking-change commit subjects. It repeats after checks so it sits directly above confirmation. Commit counts and lists respect `history_paths`; the ahead-of-origin count covers the whole branch. Breaking changes use the same conventional-commit rules as version selection: a `!` header or a `BREAKING CHANGE:` / `BREAKING-CHANGE:` line in a conventional commit. A breaking feature appears in both lists.

Dry-run uses read-only inspection and contacts origin, and GitHub through `gh` when the policy names pins. It skips tool requirements and every configured command, including preflight checks, preparation and custom suggestions. A skipped custom suggestion produces a labelled built-in estimate; it is not the custom tool's result.

Preflight commands run before confirmation and are trusted commands: they may leave artifacts even if you cancel afterward. A cancellation, EOF or interruption before preparation returns 0. An interruption during preparation or tagging prints `Release cancelled before the push.` with a summary of what was left (see [Failure handling](#failure-handling)) and returns 130 when preparation left commits, a moved or deleted release branch, a switched HEAD, a tag or uncommitted changes, or when that state could not be inspected; otherwise it returns 0. Failures return 1; argument usage errors return 2; an unattended run with nothing to release returns 3. An interruption during the push is a push failure with uncertain outcome.

## Failure handling

A failed command is reported as `COMMAND failed (CODE): DETAIL`, where DETAIL is its captured stderr (or stdout) when the command's output was captured. Engine-generated command errors redact URL userinfo, including credentials embedded in an origin URL, while retaining the operation and host. They show Git commands without the engine's fixed safety options (`--no-replace-objects --literal-pathspecs --no-optional-locks --no-lazy-fetch`). Inspection failures name what differs: a detached or unborn HEAD, a HEAD that does not contain origin's branch or the latest release tag, release tags present only locally or only on origin, and the refs that moved since the reviewed snapshot. Git receives the original destination. Configured commands and hooks still control their own terminal output.

The engine preserves local work: prepared files, commits and any created annotated tag remain available for inspection. It does not repair, reset, retry, resume or delete/recreate public tags. If a stage fails, examine the worktree, index, commit parent/scope, hook output and stage checks before deciding how to recover. A rejection after preparation can leave a clean tree with unpublished preparation commits. When preparation, the revalidation after it or tagging fails, the error's last line starts with `Curtains did not push the release.` and summarizes the local state: whether the release branch is unchanged, moved from the reviewed commit (with a commit count when it only gained commits) or deleted; where HEAD is and at which commit if it left the branch; a release tag that was created; and the uncommitted paths, quoted, the first five followed by a count. If inspecting that state fails, it asks you to inspect the repository instead. The summary covers only local state: configured commands and hooks may still have pushed. With `sign` enabled, a signing check before preflight catches an unusable key; a signing failure after it, such as an unconfirmed hardware-key touch, on a preparation commit leaves the stage's changes staged; on the tag, it leaves the preparation commits without a tag.

If the push fails or is interrupted, the remote outcome may be unknown. Inspect the remote branch and tag (including the tag's peeled target), compare them with the prepared local commit and tag object, and decide explicitly what to do. Do not assume the remote is unchanged or rerun blindly. Unsupported atomic push is an error; the engine never falls back.

With `repository`/`workflow` configured, success prints GitHub tag/workflow links and says publication runs asynchronously. `draft` adds a review link. Apart from updating policy pins, the release command does not invoke `gh`, create releases, wait for Actions, sign artifacts, upload packages or publish drafts; the consumer's tag-triggered workflow owns those steps. Inside that workflow, [`curtains ci`](#ci-commands) validates the tag and creates and verifies the GitHub release.

## Pins in a release

When the policy lists [`pins`](configuration.md#top-level-fields), the release looks up each named pin's latest GitHub release with `gh` right after inspection, on a dry run too. The summary shows `Pins: NAME OLD -> NEW` for each pin behind its latest release, or `Pins: current`. Each pending update counts as one fix, so it is enough to release on its own: a patch bump, or minor for `MAJOR.MINOR` policies. When a `suggest` command returns a version that is not newer than the latest release, pending pin updates fall back to the built-in bump.

After preflight checks and confirmation, one stage per outdated pin runs before the policy's stages. It runs [`curtains pins update`](pins.md) for the previewed version, which records the new commit and digests, rewrites the pin's references and runs its `after_update` commands, then commits the result as `fix: update NAME to VERSION`. The stage may change only `scripts/pins.json`, the pin's reference files and its declared `files`; other `after_update` output must be ignored by Git. A failed pin stage is handled like any other failed stage: its changes stay in place, and the error names the pin and suggests discarding the preparation changes and releasing with `--keep-pins`.

`--keep-pins` skips the lookup and the pin stages, and the summary shows `Pins: not checked (--keep-pins)`. A failed lookup, or a policy pin that `scripts/pins.json` does not define, stops the release before preflight checks with the same hint.

## CI commands

`curtains ci` runs inside a tag-triggered workflow, after the release command pushed the tag. It reads the same policy as the release command (`--config`, `--directory`, given before or after `ci` or the subcommand), but only its tag and publication fields: `name`, `branch`, `version_parts`, `initial_version`, `tag_prefix`, `repository`, `workflow` and `draft`. Other keys are ignored here, so a workflow pinned to an older `curtains` still reads a policy that gained release-only keys. In GitHub Actions, the [action](migration.md#replacing-a-ci-release-helper) installs `curtains`.

| Command | Behavior |
| --- | --- |
| `curtains ci validate-tag TAG [EXPECTED]` | Prints the commit of TAG. EXPECTED, if given, is a full commit SHA or `refs/tags/TAG` and must identify the same commit (`GITHUB_SHA` on a tag push). |
| `curtains ci previous-tag TAG` | Prints the highest release tag below TAG in the policy's prefix and number of version parts, or nothing when there is none. |
| `curtains ci publish TAG TARGET --notes FILE [--title TITLE] [--prerelease] [--not-latest] [ASSET...]` | Creates or completes the GitHub release for TAG; prints one line per action. |

`validate-tag` requires TAG to be a canonical release tag of the policy, present locally, annotated, and contained in `refs/remotes/origin/<branch>`. When the policy has `repository`, the origin must match it. `previous-tag` reads local tags. Both reject a shallow clone, so check out with `fetch-depth: 0` and make sure the branch and tags are fetched (`actions/checkout` then fetches all branches and tags). Errors print `curtains: MESSAGE` on stderr; URL userinfo in quoted `git` or `gh` output is redacted. Exit statuses: 0 success, 1 failure, 2 usage error, 130 interrupted.

`publish` needs `repository` in the policy, the `gh` CLI and a `GH_TOKEN` with `contents: write`. Before any write it checks that the origin matches `repository`, TARGET is a full 40- or 64-digit lowercase hex commit, the notes and assets are readable, asset paths contain no `#`, `*`, `?` or `[`, asset names start with a letter or digit, contain only letters, digits, `.`, `_`, `+` and `-`, have no `..` and do not end in `.` (names GitHub would rename on upload), and are unique, and `git ls-remote origin` shows TAG as an annotated tag peeling to TARGET. That `ls-remote` needs read access to origin, so a private repository needs credentials in the checkout. The default title is `NAME VERSION` (policy `name`, version without the prefix). The notes file becomes the release body, and a retry must pass identical notes. Relative notes and asset paths resolve against `--directory` (default: the working directory), like `--config`.

The sequence is: create a draft release (`--verify-tag`), upload the assets, check the remote tag again and that the draft holds every expected asset, then publish with `--draft=false` (plus `--prerelease` and `--latest=false` when requested). `gh` runs non-interactively. `--not-latest` applies only when the draft is published: a retry against an already-published release does not check or change which release is latest.

A retry views the release first and only completes what is missing. Title, body, tag and prerelease state must equal the expected values. Uploaded assets must match size and SHA-256 digest; any divergence, unexpected asset or duplicate name stops the run without writing, so a retry depends on a reproducible build. On a draft, assets that never finished uploading are deleted and uploaded again, missing assets are uploaded, and the draft is published. A complete published release prints `Release TAG already complete` and changes nothing. An immutable release missing assets is refused. After any change, the run ends by viewing the release again and failing if it is not complete.

With `"draft": true` in the policy, the release stays a draft for manual review and the run prints `Draft TAG ready for review`; a published release under such a policy is refused, and so is `--not-latest`, because whoever publishes the draft decides whether it becomes the latest release.

If an upload to an already-published release failed part-way, later runs refuse with "incomplete assets" rather than replacing published files. Delete the named asset, then rerun the workflow:

```sh
gh release delete-asset --repo OWNER/NAME --yes -- TAG ASSET_NAME
```

## Releasing Curtaincall itself

[The self-policy](../scripts/release.json) uses `main`, prefix `v`, and initial version `1.0.0`. Run from this checkout:

```sh
just release --dry-run
just release             # version from commit messages; or: just release minor
# equivalent source command:
LEFTHOOK=0 uv run --locked curtains
```

This uses the locked source environment, independent of a global `curtains` installation. The self-policy sets `sign`, so the releasing checkout needs a working Git signing key (`user.signingkey`, plus `gpg.format` for SSH keys); the release commit and tag are signed and the run stops early without one. [prepare_release.py](../scripts/prepare_release.py) validates the requested three-part version and exactly one manifest version in `[project]` before writing. The stage then runs `uv lock` and `just changelog VERSION`, updating only `pyproject.toml`, `uv.lock` and `CHANGELOG.md`. Preflight runs `just check-static` and `just coverage` on HEAD; the stage edits only those three files, and `just build` plus `just check-package` verify their effect on the package before the engine commits it. `just release` disables Lefthook for the engine's commits and push, since preflight already ran the same gates. The tag workflow reruns every gate on the tagged source before publishing.

Changelog generation pins `git-cliff@2.13.1` and uses [cliff.toml](../cliff.toml). Its uv tool/cache/Python directories live under `.uv`; the first invocation may download prerequisites. `just changelog VERSION` regenerates the file without creating a tag.

Package version comes from installed metadata, with `pyproject.toml` as the source manifest; there is no runtime version constant. Run the commands above from a clean checkout of `main`.

### GitHub publication

[release.yml](../.github/workflows/release.yml) runs when a `v*` tag is pushed. It requires a canonical three-part annotated tag whose commit is on `origin/main`, checks that the package version matches, and builds from that exact commit. It runs static analysis, tests with branch coverage, isolated package acceptance, and installation smoke tests of the actual wheel and sdist. The wheel is built from the sdist.

The verification job has read-only permissions. A macOS job then renders the Homebrew formula from [curtaincall.rb.in](../scripts/curtaincall.rb.in), pinned to the SHA-256 of GitHub's archive for the tag, installs it from source through a temporary tap and runs `brew test`, which checks `curtains --version` and a dry-run against a local bare remote. A separate publishing job rechecks the tag with `curtains ci validate-tag` and publishes with `curtains ci publish` a GitHub release containing the wheel, source archive and `SHA256SUMS`, with notes generated by the pinned git-cliff configuration, using GitHub's automatic `GITHUB_TOKEN`. Only after that does a final job copy the tested formula to `Formula/curtaincall.rb` in [nielsmadan/homebrew-tap](https://github.com/nielsmadan/homebrew-tap), committing only when it changed. It leaves the tap alone when it already holds a newer version, so a retry of an older tag cannot downgrade it. Tags for the same version are serialized by workflow concurrency. PyPI publication is not configured.

The tag checks are integrity checks, not an authorization boundary. On a tag push GitHub runs `release.yml` from the tagged commit, and `curtains ci` and the policy come from that commit too, so anyone who can push a `v*` tag controls what the release jobs run. Restrict who can create and move `v*` tags with a tag ruleset, and keep `HOMEBREW_TAP_TOKEN` in a deployment environment limited to `v*` tags.

The tap update needs the `HOMEBREW_TAP_TOKEN` Actions secret: a fine-grained personal access token limited to `nielsmadan/homebrew-tap` with **Contents: Read and write**. The macOS job fails before anything is published when the secret is missing. Rotate it in the repository's Actions secrets when it expires.

After a release exists, `brew install nielsmadan/tap/curtaincall` installs it. Without Homebrew, install it directly from its asset URL with `uv tool install --reinstall https://github.com/nielsmadan/curtaincall/releases/download/vVERSION/curtaincall-VERSION-py3-none-any.whl`, or download the wheel from [GitHub Releases](https://github.com/nielsmadan/curtaincall/releases) and run `uv tool install --reinstall /path/to/curtaincall-VERSION-py3-none-any.whl`. To verify a download, fetch `SHA256SUMS` into the same directory and run `shasum -a 256 -c --ignore-missing SHA256SUMS` (macOS) or `sha256sum -c --ignore-missing SHA256SUMS` (Linux); `--ignore-missing` skips assets you did not download. The checkout's `just install` remains available.

### Publication recovery

If Actions fails after the tag was pushed, fix the workflow on `main` if needed and use **Run workflow** on the Release workflow, selecting branch `main` and the existing tag. The CLI equivalent is:

```sh
gh workflow run release.yml --ref main -f tag=v1.0.0
```

Manual dispatch uses the workflow, `curtains ci` (installed by the action from the same checkout) and formula template from `main` while rebuilding the original tagged source, so a formula fix ships without a new version. It never retags a newer commit. `curtains ci publish` accepts an identical existing release, uploads only missing assets, and finishes a matching draft, as described under [CI commands](#ci-commands). It compares existing asset sizes and SHA-256 digests and refuses divergent title, notes, assets, prerelease state or moved tags. [release_workflow.py](../scripts/release_workflow.py) provides the manifest version check, formula rendering and the tap's downgrade guard. A retry therefore depends on the rebuild being byte-identical: hatchling's wheel and sdist from the same commit are reproducible, so keep build-time inputs free of timestamps and host paths. An immutable release missing assets also fails, and so does a published release with an incomplete asset (see the manual deletion above). A retry after a successful publication but failed tap update re-tests the formula, accepts the existing release, and updates the tap. Inspect the failed run and existing release before repairing anything; rerunning `just release` is not a publication retry.
