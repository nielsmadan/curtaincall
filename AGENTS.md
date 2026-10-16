# Curtaincall

Read [docs/overview.md](docs/overview.md) for configuration, release, architecture and contributor entry points. After completing a feature, run `doc --update` to keep these canonical docs current.

Run `just check` for static checks and unit/Git/CLI integration tests. Run `just coverage`, `just build` and `just check-package` before a release or packaging change. The packaging gate performs isolated installations and may download declared build prerequisites; ordinary checks exclude it.

Keep the runtime standard-library-only. The CLI is the supported interface; there is no public Python API. Preserve read-only dry-run, strict historical tag validation, annotated tags, and atomic explicit branch/tag publication. Configured commands and hooks are trusted; stage allowlists are integrity checks.

Tests must use disposable repositories and local bare remotes. Packaging tests isolate tool/bin/cache/Python directories; never install globally or publish real refs during verification. `docs/development.md` explains the isolated development environment.

This repository's AGENTS.md is hand-maintained and canonical. There is no Loadout configuration to initialize or sync.
