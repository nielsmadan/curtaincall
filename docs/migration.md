# Migrating a release wrapper

Install `curtains` first (see the README). In a consumer, retain its release policy, preparation scripts, changelog configuration and tag-triggered publication CI. Replace only the local orchestration wrapper with `curtains`, keeping its command arguments explicit. Existing preflight and stage commands remain consumer-owned and still need their tools.

A consumer's Just recipe can forward arguments:

```just
release *args:
    @curtains {{args}}
```

Run `just release --dry-run` to inspect the policy and history before a real release. Compare required tools, preflight checks, stage file scopes and commit messages with the old wrapper. A dry-run cannot verify consumer commands because it deliberately skips them. Use an isolated disposable clone for preparation testing; preserve normal publication triggers in the real repository.

For Flyrail's nested Python policy, select it from the Git root explicitly:

```sh
curtains --config python/scripts/release.json --dry-run
```

From inside `python`, use `--config scripts/release.json`; all configured files and commands still resolve from the Git root. No nested-policy guessing occurs.

## Replacing a CI release helper

A consumer's tag-triggered workflow often carries a helper script that validates the tag, finds the previous tag and publishes the GitHub release. `curtains ci` replaces those commands (see [CI commands](release.md#ci-commands)); manifest checks and registry publishing stay in the consumer. Check out with `fetch-depth: 0`, add the action after the checkout, and give the publish job `contents: write`:

```yaml
- uses: actions/checkout@<sha>
  with:
    fetch-depth: 0
- uses: nielsmadan/curtaincall@<full SHA of a release tag> # vX.Y.Z
- env:
    RELEASE_TAG: ${{ github.ref_name }}
  run: curtains ci validate-tag "$RELEASE_TAG" "$GITHUB_SHA"
```

Pass tag names and other workflow values to `run:` through `env:`, never as `${{ }}` expressions inside the script, which Actions substitutes into the shell text before it runs.

Pin the full commit SHA of a release tag with the version in a trailing comment, or the exact release tag when a workflow lockfile from `gh actions-lock` locks it; Dependabot bumps either. The pin is the `curtains` version: the action installs `curtains` from its own checkout and has no version input. It has no outputs either; it puts `curtains` on `PATH` for later steps and leaves the job's `python3` unchanged. The action brings its own Python, so the job needs no `setup-python` for it. The policy must set `repository` for `publish` and for the origin check.

| Fork command | `curtains ci` |
| --- | --- |
| `validate-tag [--component C] TAG [SHA]` | `validate-tag TAG [EVENT]`; the component is the policy's `tag_prefix`, so use one policy per component |
| `previous-tag TAG` | `previous-tag TAG`; reads the policy's prefix and component count |
| `publish TAG TITLE NOTES TARGET ASSETS [--prerelease]` | `publish TAG TARGET --notes NOTES [--title TITLE] [--prerelease] [--not-latest] [ASSETS]` |

Without `--title`, the title is the policy `name` and version. `publish` creates a draft, uploads, then publishes, and a retry completes what is missing.

- Flyrail: one policy per component, selected with `--config` (for example `--config python/scripts/release.json`), each with its own `tag_prefix`. Pass `--not-latest` always. The policy names (`Flyrail for Python`, `Flyrail for Rust`, `Flyrail for Swift`) already produce the existing release titles, so no `--title` is needed. The fork's "at least one asset unless Swift" check moves into the workflow; `curtains ci publish` accepts zero assets.
- Splashdown: `--title "splashdown $VERSION"`; for prereleases pass `--prerelease` and `--not-latest` together, since the fork also passed `--latest=false` for them.
- Pratfall: `--title "pratfall $VERSION"`. Prerelease state is now verified on retry as well as at creation.

The `{tag}` and `{tag_prefix}` placeholders ([configuration](configuration.md#stages)) and any other key added since a policy was written need the new version in both places: update the local `curtains` install and the action pin together, or one side rejects the policy.

## Standalone policy fixtures

The copied [fixtures](../tests/fixtures/policies) require no sibling checkout. They are examples of policy shapes, not bundled consumer build tools or migrated repositories.

| Fixture | Distinct preparation / publication shape |
| --- | --- |
| [ShortcutKit](../tests/fixtures/policies/ShortcutKit.json) | Tag only, Swift Package Manager consumers. |
| [harlequin](../tests/fixtures/policies/harlequin.json) | `master` branch, two-part versions, tag only. |
| [ShortcutField](../tests/fixtures/policies/ShortcutField.json) | README version preparation for Swift Package Manager consumers. |
| [juggler](../tests/fixtures/policies/juggler.json) | Two stages; later installer pin uses the version commit's revision. |
| [hooklinesinker](../tests/fixtures/policies/hooklinesinker.json) | Initial `1.0.0`, Cargo manifest/lock preparation and binary publication. |
| [mouthfeel](../tests/fixtures/policies/mouthfeel.json) | npm manifest/lock/dist preparation and asynchronous draft release. |
| [loadout](../tests/fixtures/policies/loadout.json) | Custom git-cliff version suggestion and Python preparation. |
| [pratfall](../tests/fixtures/policies/pratfall.json) | Python manifest/lock/changelog and custom suggestion. |
| [splashdown](../tests/fixtures/policies/splashdown.json) | Python preparation and downstream release/formula workflow. |
| [flyrail-python](../tests/fixtures/policies/flyrail-python.json) | `python-v` namespace, `python`/`spec` history and stage package checks. |

The example tests validate every fixture unchanged and execute representative dimensions using synthetic commands rather than Xcode, Cargo or npm. The fixtures cover preparation only; CI tag validation and GitHub release publication are covered by [replacing a CI release helper](#replacing-a-ci-release-helper).

Migration intentionally tightens inspection: all historical matching tags must agree, not just the latest; missing objects are never fetched implicitly; chained destination rewrites are rejected; dry-run never runs a custom suggestion. The engine's atomic push covers branch plus new tag only, and preparation failures preserve artifacts. Review these differences before replacing a wrapper.
