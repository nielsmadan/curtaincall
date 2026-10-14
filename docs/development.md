# Development

Use Python 3.13+, Git 2.45+, uv, Just and Lefthook. `just setup` runs locked dependency synchronization, installs repository hooks, then runs doctor. `just doctor` checks tools, the environment and hook configuration/executables. Hook installation changes this checkout's hook setup.

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

`just clean` removes generated builds, reports and ordinary test caches. It leaves `.uv` prerequisites and `.cache` alone. Keep source-release instructions in [release.md](release.md); configuration and engine changes belong in their corresponding canonical pages.
