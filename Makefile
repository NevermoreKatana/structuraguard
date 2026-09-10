UV ?= uv
RUN = $(UV) run --locked --no-sync
PYTHON_PATHS = packages/structuraguard/src packages/structuraguard/tests scripts

.PHONY: sync lock-check format lint typecheck test test-integration test-database test-security docs build test-build check

sync:
	$(UV) lock --check
	$(UV) sync --all-packages --locked --group dev --group docs

lock-check:
	$(UV) lock --check

format:
	$(RUN) ruff check --fix $(PYTHON_PATHS)
	$(RUN) ruff format $(PYTHON_PATHS)

lint:
	$(RUN) ruff format --check $(PYTHON_PATHS)
	$(RUN) ruff check $(PYTHON_PATHS)

typecheck:
	$(RUN) mypy

test:
	$(RUN) pytest

test-integration:
	$(RUN) pytest -m 'integration and not database_integration'

test-database:
	$(RUN) pytest -W error::sqlalchemy.exc.SAWarning -m database_integration packages/structuraguard/tests/integration/database

test-security:
	$(RUN) pytest packages/structuraguard/tests/security

docs:
	$(RUN) mkdocs build --strict --clean

build:
	$(UV) build --offline --no-python-downloads --package structuraguard --out-dir dist --clear --no-create-gitignore --no-build-isolation

test-build: build
	$(RUN) python scripts/verify_distribution.py dist

check: lock-check lint typecheck test docs test-build
