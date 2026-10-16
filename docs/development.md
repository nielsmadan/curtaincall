# Development

Use Python 3.13+, Git 2.45+, uv, Just and Lefthook. `just setup` runs locked dependency synchronization, installs repository hooks, then runs doctor. `just doctor` checks tools, the environment and hook configuration/executables. Hook installation changes this checkout's hook setup.

The [pre-commit hook](../lefthook.yml) checks formatting and lint on staged Python files. Pre-push runs `just check-static` then `just coverage` (the whole non-packaging suite, including the coverage threshold), stopping at the first failure; `just release` skips the hooks. CLI installation through `just install` is separate from this contributor setup.

Doctor resolves relative or empty `PATH` entries from the checkout root. Signed-history regression tests use `ssh-keygen` to create a temporary SSH signing key. Terminal cancellation tests kill and reap their child on timeout before closing the PTY.

For isolated agent work, keep downloads and caches inside the checkout:

```sh
export UV_CACHE_DIR="$PWD/.uv/cache"
export UV_PYTHON_INSTALL_DIR="$PWD/.uv/python"
uv sync --locked --all-groups
just check
just coverage
just build
just check-package
```

`just check` runs Ruff lint/format validation, cyclic-import checking, strict mypy over source/tests/scripts, and pytest unit plus real Git/CLI integration tests. Pytest treats warnings as errors. Coverage measures runtime branch coverage with an 80% minimum. Formatting changes use `just format`; `just lint` and `just typecheck` run individual gates.

`just build` creates the sdist, then builds the wheel from it. The sdist ships canonical docs, policy fixtures, tests and contributor/release tooling; the wheel contains only runtime code and package metadata. Build backends and isolated installation prerequisites can require downloads.

`just check-package` runs distribution and installation acceptance separately from routine tests. It uses temporary directories under `.cache`, local bare Git remotes, isolated uv tool/bin/cache/Python directories and the existing interpreter with automatic Python downloads disabled. Tests prove snapshot installation, same-version reinstallation, editable behavior, uninstall and installed CLI use outside the source tree. No machine-wide installation or real release is needed.

`just install`, `just install-editable` and `just uninstall` operate on the user's uv tool installation. Use those intentionally for your own installation; tests exercise the recipes with isolated directories. CI checks macOS and Linux with locked dependencies and the packaging gate; Linux also checks coverage and builds packages.

[CI](../.github/workflows/ci.yml) runs on branch pushes and pull requests with pinned actions, read-only repository permissions and 30-minute job limits. Coverage appears in the job summary and a downloadable XML/HTML artifact; distributions are uploaded separately. Codecov uploads follow the repository's `CODECOV_TOKEN` secret or its public-repository tokenless-upload configuration, and upload failures do not fail CI. The local 80% coverage threshold remains mandatory. See [Codecov's setup instructions](https://github.com/codecov/codecov-action#usage); Dependabot needs its own `CODECOV_TOKEN` secret when using token authentication.

[Dependabot](../.github/dependabot.yml) updates the uv lockfile and GitHub Actions every Monday at 08:00 Europe/Berlin, assigns PRs to `nielsmadan`, and groups patch/minor updates per ecosystem. Major updates stay separate. Both ecosystems have a five-PR limit.

`just clean` removes generated builds, reports and ordinary test caches. It leaves `.uv` prerequisites and `.cache` alone. Keep source-release instructions in [release.md](release.md); configuration and engine changes belong in their corresponding canonical pages.
