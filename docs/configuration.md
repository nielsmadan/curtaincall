# Release configuration

The parser in [config.py](../src/curtaincall/config.py) rejects duplicate or unknown keys, incorrect types and malformed templates before release commands run. JSON policy is trusted repository code: argv execution prevents shell interpretation, but a configured executable can still perform arbitrary actions.

## Location and paths

`--directory DIR` selects the starting directory, defaulting to the current directory. Git determines the worktree root. The default policy is `<root>/scripts/release.json`; a relative `--config PATH` resolves from the starting directory and must resolve inside that worktree. Installed package location never selects the target repository. Nested policies require explicit selection.

Commands run at the Git root. Stage files and history paths are literal root-relative paths: no absolute paths, `.`/`..` segments, `.git`, backslashes, glob or pathspec syntax. Symlinks must resolve inside the root and outside `.git`. A directory allows its descendants at slash boundaries: `dist` includes `dist/file`, never `dist-other`.

Required-tool lookup uses the same root as command execution: relative `PATH` entries resolve from the Git root, and an empty entry denotes that root. Search order is preserved. When `PATH` is absent, lookup and execution use the system default search path (`os.defpath`); an explicitly empty `PATH` still denotes the Git root.

## Top-level fields

| Field | Meaning / default |
| --- | --- |
| `name` | Required nonempty display name. |
| `initial_version` | Required canonical numeric version for the first release. |
| `branch` | Release branch; `main`. Must be a valid Git branch name. |
| `components` | Integer `2` or `3`; `3`. Booleans are rejected. |
| `tag_prefix` | Nonempty prefix; `v`. Prefix + initial version must form a valid tag ref. |
| `paths` | History scope, literal path array; `[]` means whole repository. |
| `tools` | Executables required for a real run; `[]`. Slash-containing names resolve from root. |
| `checks` | Preflight argv arrays; `[]`. Run before confirmation. |
| `stages` | Sequential preparation stages; `[]` means tag only. |
| `suggest` | Optional single argv array returning a canonical version, optionally prefixed. |
| `publication` | Success description; `Version tag pushed.` |
| `repository` | Optional GitHub `owner/name`; requires origin identity to match exactly. |
| `workflow` | Optional `.yml`/`.yaml` filename; requires `repository`. Prints an asynchronous workflow link. |
| `draft` | Boolean; `false`. `true` requires `workflow`; prints a draft review link. |

Every command is a nonempty array of strings. The executable must be nonempty; later arguments may be empty. NUL is rejected. There are no shell command strings. `checks` and `suggest` arguments are literal, with no interpolation.

## Stages

Each stage requires nonempty `files` and `commands` arrays and a nonempty `message`; `checks` defaults to `[]`. Only stage command/check arguments and commit messages interpolate:

| Field | Value |
| --- | --- |
| `{version}` | Selected numeric version, without tag prefix. |
| `{revision}` | HEAD at this stage's start; later stages see earlier preparation commits. |
| `{python}` | Curtains' own interpreter, for standard-library-only helper scripts. |

Scripts needing consumer dependencies should use the repository's package runner, for example `uv run --locked python scripts/prepare_release.py {version}`.

Escape literal braces as `{{` and `}}`. Attribute/index access, conversions and format specs are rejected.

```json
{
  "name": "Example",
  "initial_version": "0.1.0",
  "stages": [{
    "files": ["pyproject.toml", "uv.lock", "CHANGELOG.md"],
    "commands": [["{python}", "scripts/prepare_release.py", "{version}"], ["uv", "lock"]],
    "checks": [["just", "check-package"]],
    "message": "chore: release {version}"
  }]
}
```

The preparation script is consumer-owned. The stage allowlist must cover everything its commands and hooks change. The engine commits only changed declared files; a no-change stage creates no commit. See the [self-release policy](../scripts/release.json) for changelog generation and prepared-package verification.

## Version selection

Without release tags, propose `initial_version`. Otherwise `feat` bumps minor, `fix`/`perf` bumps patch, and a breaking conventional header or footer bumps major (minor while a three-part version is `0.x`). Two-part policies use minor for fixes and reject `patch`. Chores alone produce no automatic bump. Numeric components have no leading zeros; prereleases are unsupported.

A CLI override accepts `patch`, `minor`, `major`, or a canonical version with an optional configured prefix. An existing release requires a strictly greater version and relevant commits even with an override. A real custom suggestion runs only after an existing release and without an override. Invalid or failing output aborts; a valid nonincreasing suggestion means no automatic bump. Dry-run skips it and labels its conventional estimate.
