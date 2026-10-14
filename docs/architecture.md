# Architecture

The CLI is the compatibility boundary. Internal Python functions and dataclasses are not a public library API. The runtime uses only the standard library; consumer-specific preparation and publication remain outside the engine.

[config.py](../src/curtaincall/config.py) turns strict JSON into frozen policy values; [versions.py](../src/curtaincall/versions.py) makes deterministic version decisions from inspected history. [git.py](../src/curtaincall/git.py) owns subprocess execution, root/destination discovery and read-only Git inspection. [release.py](../src/curtaincall/release.py) orders those boundaries and controls stage commits, tagging and publication, plus previews, confirmation and progress output. [cli.py](../src/curtaincall/cli.py) owns arguments, help, installed version metadata, top-level errors and exit status.

`ReleaseState.latest` holds the canonical numeric version without a prefix; `TagState.name` retains the full tag name. CLI and custom-suggestion inputs accept a configured prefix only when the input is not already a canonical numeric version. This order also supports numeric tag prefixes.

The lifecycle is inspect → propose → preflight checks → confirm → revalidate → prepare stages → revalidate → annotated tag → atomic push → report. Dry-run ends after preview, without executing any configured command. Loaded configuration stays fixed throughout a run.

The reviewed snapshot binds HEAD, branch, cleanliness, origin destination, remote release branch and the entire matching historical tag namespace. Preparation may advance HEAD only through the engine's own single-parent stage commits. Origin and historical refs are checked again before tagging. Unrelated branches and tag namespaces are outside this snapshot.

Stage integrity compares changed paths without rename heuristics, validates directory boundaries and symlinks, and captures the staged tree before commit. Hooks remain enabled. Each stage commit must have exactly the captured HEAD as its single parent and change only allowed paths. If hooks change allowed bytes, stage checks rerun against the resulting commit; without stage checks only scope/commit integrity is established. A dirty result, switched branch or extra commit stops the release.

The push targets the validated destination with exact prepared commit and annotated tag object OIDs, `--atomic --no-follow-tags --recurse-submodules=no`. It never forces, retries, rolls back or falls back to a non-atomic push. Atomicity covers the two remote refs, not local preparation, a global namespace lock or asynchronous publication. Concurrent actors can still change refs after revalidation.

Engine Git operations use `git_command` for literal pathspecs, replacement-ref suppression, disabled optional locks and disabled lazy fetching. `command_environment` clears inherited Git context while preserving non-Git variables and explicit Git authentication, transport and configuration controls. `run_command` captures bytes and decodes with `os.fsdecode`, without universal-newline conversion. Callers may strip scalar output, but must preserve NUL-delimited filename output so CR/LF and undecodable filename bytes round-trip.

Historical tag objects are peeled and validated together in one batch per inspection, including nested annotated tags. Missing history is an inspection failure, not an invitation to fetch. Commit-message extraction suppresses signature display so Git's display settings cannot alter version selection. These boundaries are tested with disposable repositories and bare local remotes, including a discriminating non-atomic push control.
