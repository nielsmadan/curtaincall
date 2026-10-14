[private]
default:
    @just --list

setup:
    @uv sync --locked --all-groups
    @lefthook install
    @just doctor

doctor:
    @python3 scripts/doctor.py

test:
    @uv run --locked pytest -m 'not packaging' -q

coverage:
    @uv run --locked pytest -m 'not packaging' --cov --cov-report=term-missing --cov-report=html --cov-report=xml -q

check-package:
    @uv run --locked pytest -m packaging -q

lint:
    @uv run --locked ruff check

lint-imports:
    @uv run --locked pylint src/curtaincall

format:
    @uv run --locked ruff format

typecheck:
    @uv run --locked mypy

check-static:
    @uv run --locked ruff check
    @uv run --locked ruff format --check
    @uv run --locked pylint src/curtaincall
    @uv run --locked mypy

check: check-static test

build:
    @rm -rf dist build
    @uv build --sdist
    @uv build --wheel dist/curtaincall-*.tar.gz

install:
    @uv tool install --reinstall --force .

install-editable:
    @uv tool install --reinstall --force --editable .

uninstall:
    @uv tool uninstall curtaincall

clean:
    @rm -rf dist build .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml

release *args:
    @uv run --locked curtains {{args}}

changelog version:
    @env UV_TOOL_DIR="$PWD/.uv/tools" UV_CACHE_DIR="$PWD/.uv/cache" UV_PYTHON_INSTALL_DIR="$PWD/.uv/python" uvx git-cliff@2.13.1 --config cliff.toml --tag 'v{{version}}' -o CHANGELOG.md
