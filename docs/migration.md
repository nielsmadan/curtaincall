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

The example tests validate every fixture unchanged and execute representative dimensions using synthetic commands rather than Xcode, Cargo or npm. Real consumer publication remains in its existing CI helper.

Migration intentionally tightens inspection: all historical matching tags must agree, not just the latest; missing objects are never fetched implicitly; chained destination rewrites are rejected; dry-run never runs a custom suggestion. The engine's atomic push covers branch plus new tag only, and preparation failures preserve artifacts. Review these differences before replacing a wrapper.
