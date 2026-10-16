# Curtaincall

`curtains` prepares local releases from a repository-owned JSON policy, creates an annotated tag, and atomically pushes the prepared branch and tag. Python 3.13+; macOS and Linux; no runtime dependencies.

Install from this checkout with [uv](https://docs.astral.sh/uv/) and [Just](https://just.systems/):

```sh
just install
curtains --help
curtains --version
```

`just install` installs a snapshot; rerun it after source changes, including changes at the same version. `just install-editable` tracks source edits immediately. `just uninstall` removes the tool. Ensure uv's tool executable directory is already on your PATH. These instructions use local source; they do not assume a PyPI publication.

Curtaincall uses its own `just release` command. Its tag-triggered workflow publishes tested wheels and source archives to [GitHub Releases](https://github.com/nielsmadan/curtaincall/releases). See [self-release and recovery](docs/release.md#releasing-curtaincall-itself) for the first release and workflow retries.

For development, install [Lefthook](https://lefthook.dev/installation/) and run `just setup` after cloning. It installs locked development dependencies and Git hooks, then checks the checkout with `just doctor`. See [development](docs/development.md) for the validation commands.

In the repository you want to release, add `scripts/release.json`:

```json
{
  "name": "Example",
  "initial_version": "0.1.0",
  "checks": [["just", "check"]]
}
```

```sh
curtains --dry-run
curtains                 # review and confirm interactively
curtains minor           # choose a bump
curtains 1.2.3 --yes      # explicit unattended confirmation
```

Use Git 2.45+ and an origin server supporting atomic pushes. Start on the configured branch (default `main`) with a clean, complete worktree and matching local/remote release tags. Inspection contacts origin but never fetches missing objects. Dry-run executes no policy commands, including custom suggestions; its built-in estimate is labelled when a suggestion is skipped.

Policies can prepare manifests and changelogs in sequential stages. Commands and Git hooks are trusted code; file allowlists detect mistakes and are not a sandbox. Cancellation and failures preserve any artifacts already created. A failed push can have an unknown remote outcome: inspect refs before further action.

Read [configuration](docs/configuration.md) for the schema, [release behavior](docs/release.md) for prerequisites and recovery, and [migration](docs/migration.md) for the ten standalone policy examples. Contributors: [development](docs/development.md), [architecture](docs/architecture.md), and the [docs index](docs/overview.md).
