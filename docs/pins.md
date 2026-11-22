# Pins

`curtains pins` manages pins of other repositories' GitHub release assets, for a project that ships or installs another project's released binary or wheel. A pin records the release version, the commit its tag points to, and the sha256 of each asset the project uses. Updating a pin rewrites the files that mention the version and runs the project's follow-up commands, so those steps live in the pins file instead of in someone's memory.

The pins file is `scripts/pins.json` at the Git root; `--file PATH` selects another file inside the worktree, relative to the starting directory (`--directory`, default: the working directory). The file is optional until the first `add`.

## Commands

| Command | Behavior |
| --- | --- |
| `curtains pins check [NAME...] [--strict] [--json]` | Compares each pin with its repository's latest release and prints one line per pin. Without `--strict` it exits 0 even when a pin is behind or a lookup fails (failures go to stderr). `--strict` exits 1 in either case, for a release `preflight`. `--json` prints `name`, `repository`, `pinned`, `latest`, `outdated` and `error` per pin. |
| `curtains pins update NAME [VERSION]` | Pins the latest release, or VERSION (with or without the tag prefix), then rewrites every reference, writes the pins file and runs `after_update`. Without VERSION it does nothing when the pin is current or newer than the latest release. An explicit VERSION may be older. Re-pinning the same version only verifies that the release's digests still match the pin. |
| `curtains pins add NAME OWNER/NAME ASSET... [--version VERSION] [--tag-prefix PREFIX]` | Adds a pin for the latest release or VERSION. ASSET names may contain `{version}`. Add `references` and `after_update` to the file afterwards. |
| `curtains pins fetch NAME --output DIR [--asset ASSET]...` | Downloads the pinned assets (or the named ones, with `{version}` filled in) into DIR and verifies each against its pinned digest. A file is moved into DIR only after it verifies. A relative DIR resolves against `--directory`. Downloaded files are not made executable. |

Every command needs the `gh` CLI, authenticated for private repositories. Errors print `curtains: MESSAGE` on stderr. Exit statuses: 0 success, 1 failure, 2 usage error, 130 interrupted.

## File format

```json
{
  "hooklinesinker": {
    "repository": "nielsmadan/hooklinesinker",
    "version": "1.5.2",
    "commit": "bd8646c29df59445bfd89fc449cbb1ed046c428a",
    "assets": {
      "hooklinesinker-macos-universal": "sha256:d97ceb246b65a4b9b7f6b60b0d9d751230e83c1ddd4d69a70923b555912984ab"
    },
    "references": [
      { "file": "scripts/install-remote.sh", "pattern": "HOOKLINESINKER_VERSION:-v{version}" }
    ],
    "after_update": [["just", "build"], ["just", "verify-hooklinesinker"]]
  }
}
```

| Field | Meaning |
| --- | --- |
| key | Pin name: lowercase letters, digits, `-` and `_`. |
| `repository` | GitHub `owner/name`. |
| `tag_prefix` | Optional; `v`. The release tag is the prefix followed by the version. |
| `version` | Canonical numeric version, any number of parts. Prerelease tags cannot be pinned. |
| `commit` | Full commit SHA the release tag points to, for source builds and provenance. |
| `assets` | Asset name to `sha256:<hex>`. Names may contain `{version}`, so an asset like `tool-{version}.whl` follows updates. |
| `references` | Optional. On update, every occurrence of `pattern` formatted with the old version is replaced by `pattern` formatted with the new one. `pattern` must contain `{version}`, and the file must contain the old value, or the update stops before writing anything. `file` is a literal repository-relative path. |
| `after_update` | Optional argv arrays run from the Git root after the files are written, with `{version}` and `{tag}` filled in. If one fails, the updated files are left for review and the command exits 1. |

Unknown keys, duplicate keys and malformed values are rejected. Literal braces in templates are written `{{` and `}}`. Curtains rewrites the whole file on update, two-space indented, in pin order.

Consumers read the file directly from their build scripts: for example, `json.load(open("scripts/pins.json"))["hooklinesinker"]["version"]`.

## Integrity

Digests come from GitHub's own asset `digest` field, which GitHub computes when the asset is uploaded. For an asset without one, `update` and `add` download the asset and hash it. `fetch` always verifies downloaded bytes against the committed digest. So a release asset replaced after pinning is detected when it is fetched, and `update NAME VERSION` to the same version reports it.

To block a release while a pin is behind, add `["curtains", "pins", "check", "--strict"]` to the policy's `preflight`. That makes the release depend on GitHub being reachable; without `--strict`, the check only warns.
