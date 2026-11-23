# Curtaincall

`curtains` is a small command-line tool for cutting releases from your own machine. It reads a release policy checked into the repository and, based on your commit history, proposes the next version. It then runs your checks and preparation steps, commits the result and pushes the release commit together with an annotated tag in one atomic push. A companion GitHub Action takes over once the tag lands: it validates the tag and publishes the GitHub release.

It's an internal tool. I built it to replace the one-off release scripts I had in each of my projects, and those projects are its main users. The code is public and MIT-licensed, so anyone can use it.

## What it does

- **Proposes the version.** It reads [Conventional Commits](https://www.conventionalcommits.org/) since the last release tag. `feat` bumps minor, `fix` and `perf` bump patch, and breaking changes bump major. You can override with `patch`, `minor`, `major` or an exact version.
- **Runs your release steps in order.** Preflight checks run first. Then come preparation stages: version bumps, lockfile updates, changelog generation, anything a command can do. Each stage declares which files it may change and becomes its own commit.
- **Fails closed.** It refuses to start without a clean worktree on the release branch, or when local and remote release tags disagree. `--dry-run` is strictly read-only, and nothing reaches the remote unless every step succeeds.
- **Publishes atomically.** It pushes the branch and the annotated (optionally signed) tag together, or not at all.
- **Hands off to CI.** `curtains ci` validates the pushed tag, finds the previous tag for release notes, and creates or completes the GitHub release with assets. Re-running a failed workflow is safe.

## Who it's for

Curtaincall may suit you if:

- you maintain several small projects and want one consistent release flow, without copying a release script into each of them;
- your releases are made locally by a person, and a tag-triggered workflow does the publishing;
- you already write Conventional Commits, or are happy to start;
- your projects mix languages and toolchains (Python, Swift, Rust, Node…). The policy only runs commands, so the tool doesn't care what they build.

Look elsewhere if you want fully automated releases on every merge (semantic-release, release-please), registry publishing built in, prerelease versions, or Windows support.

## Quick start

Install with [Homebrew](https://brew.sh/):

```sh
brew install nielsmadan/tap/curtaincall
```

Add `scripts/release.json` to the repository you want to release:

```json
{
  "name": "Example",
  "initial_version": "0.1.0",
  "preflight": [["just", "check"]],
  "stages": [
    {
      "files": ["pyproject.toml", "CHANGELOG.md"],
      "commands": [
        ["{python}", "scripts/set_version.py", "{version}"],
        ["git-cliff", "--tag", "{tag}", "-o", "CHANGELOG.md"]
      ],
      "message": "chore: release {version}"
    }
  ]
}
```

Then, from the release branch:

```sh
curtains --dry-run       # inspect the proposal; runs nothing
curtains                 # run checks, review, confirm, release
curtains minor           # force a bump
curtains 1.2.3 --yes     # explicit version, no prompt
```

A policy with no `stages` just tags the current commit. [Configuration](docs/configuration.md) covers every field, and the [policy examples](docs/migration.md#standalone-policy-fixtures) show real policies for Python, Swift, Rust and multi-package repositories.

## In GitHub Actions

The action installs `curtains` at the version of the tag you pin. A tag-triggered workflow can then use it like this:

```yaml
- uses: actions/checkout@v7.0.1
  with:
    fetch-depth: 0
- uses: nielsmadan/curtaincall@v2.2.0
- run: curtains ci validate-tag "$GITHUB_REF_NAME" "$GITHUB_SHA"
- run: curtains ci publish "$GITHUB_REF_NAME" "$GITHUB_SHA" --notes notes.md dist/*
  env:
    GH_TOKEN: ${{ github.token }}
```

Pin exact release tags and lock them to their commits with [`gh actions-lock`](https://gh.io/actions-lockfile), or pin the full commit SHA of a release tag with the version in a trailing comment. `publish` needs `"repository": "owner/name"` in the policy and `contents: write` on the job. The full workflow is in [replacing a CI release helper](docs/migration.md#replacing-a-ci-release-helper), and command details are in [CI commands](docs/release.md#ci-commands).

## Pinning other projects' releases

A project that ships another project's release assets can pin them in `scripts/pins.json`:

```sh
curtains pins add hooklinesinker nielsmadan/hooklinesinker hooklinesinker-macos-universal
curtains pins check                        # which pins are behind their latest release?
curtains pins update hooklinesinker        # new digests, rewritten references, follow-up commands
curtains pins fetch hooklinesinker --output build/vendor
```

Each pin records the release version, its tag commit and every asset's sha256, so `fetch` verifies downloaded bytes against the committed digest. Name a pin in the release policy's `pins` and every `curtains` release updates it first, committing `fix: update NAME to VERSION`; `--keep-pins` skips that. See [pins](docs/pins.md) and [pins in a release](docs/release.md#pins-in-a-release).

## Requirements

- macOS or Linux, Python 3.13+ and Git 2.45+. The Homebrew formula brings its own Python and Git.
- An origin that supports atomic pushes (GitHub does).
- No runtime dependencies beyond the standard library.

## Other installs

Curtaincall is not on PyPI. Without Homebrew, install a wheel from [GitHub Releases](https://github.com/nielsmadan/curtaincall/releases) with [uv](https://docs.astral.sh/uv/), replacing `X.Y.Z` with the latest version:

```sh
uv tool install https://github.com/nielsmadan/curtaincall/releases/download/vX.Y.Z/curtaincall-X.Y.Z-py3-none-any.whl
```

To upgrade, rerun the same command with `--reinstall` and the new URL; `uv tool upgrade` does not move a URL install.

From a checkout, `just install` installs a snapshot, `just install-editable` tracks source edits and `just uninstall` removes it.

## Documentation

- [Configuration](docs/configuration.md): the policy schema and version selection.
- [Releases and recovery](docs/release.md): prerequisites, prompts, exit codes, failure handling and CI commands.
- [Migration](docs/migration.md): replacing an existing release wrapper or CI helper, plus example policies.
- [Pins](docs/pins.md): pinning, updating and fetching other repositories' release assets.
- [Development](docs/development.md) and [architecture](docs/architecture.md): working on Curtaincall itself. Run `just setup` after cloning (it needs [Lefthook](https://lefthook.dev/installation/)).

Curtaincall releases itself with `just release`; see [self-release and recovery](docs/release.md#releasing-curtaincall-itself).

## License

[MIT](LICENSE)
