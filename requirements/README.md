# Python 3.13 dependency locks

The root application and `binance-snapshot/` have separate production and
development dependency sets. Each generated lock pins direct and transitive
packages with SHA-256 artifact hashes. Install a lock with pip's
`--require-hashes --only-binary=:all:` options. The root locks also pin
`setuptools`, the build backend required by `pyproject.toml`; install the local
project afterward with `--no-deps --no-build-isolation`.

These locks were resolved with Python 3.13 and pip-tools 7.6.2 against PyPI.
Hash-checked wheel downloads were verified for Python 3.13 on macOS ARM64 and
manylinux x64 and ARM64. GitHub's `ubuntu-latest` jobs use x64. Render's native
runtime uses Debian 12; verify the live service's Python version and architecture
before its next deployment. A change to Python minor version or target platform
requires a fresh resolution and installation check.

## Regenerate

Run from the repository root in a disposable Python 3.13 environment. The
existing lock files seed their versions; use `--upgrade` only when an intentional
dependency update has been reviewed.

```bash
lock_venv="$(mktemp -d)"
python3.13 -m venv "$lock_venv"
"$lock_venv/bin/python" -m pip install 'pip==25.0.1' 'pip-tools==7.6.2' 'setuptools==84.2.1' 'wheel==0.48.0'
flags=(--no-build-isolation --no-header --no-annotate --generate-hashes --allow-unsafe --pip-args '--only-binary=:all:' --index-url https://pypi.org/simple)
"$lock_venv/bin/pip-compile" "${flags[@]}" --build-deps-for wheel --output-file requirements/root-production-py313.txt pyproject.toml
"$lock_venv/bin/pip-compile" "${flags[@]}" --build-deps-for wheel --extra dev --output-file requirements/root-development-py313.txt pyproject.toml
"$lock_venv/bin/pip-compile" "${flags[@]}" --output-file binance-snapshot/requirements-production-py313.lock binance-snapshot/requirements.txt
"$lock_venv/bin/pip-compile" "${flags[@]}" --output-file binance-snapshot/requirements-development-py313.lock binance-snapshot/requirements-dev.txt
```

pip-tools obtains hashes from PyPI distribution metadata. Review every version
and hash change, scan the locked versions for advisories, then test a fresh
hash-checked install of each lock on Python 3.13. Validate Linux x64 and ARM64
wheel selection with `pip download --require-hashes --only-binary=:all:` and the
appropriate `--platform`, `--python-version 3.13`, `--implementation cp`, and
`--abi cp313` options. Run `pip check`, both offline test suites, Ruff checks,
and the PostgreSQL integration tests before publishing updated locks.
