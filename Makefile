.PHONY: setup test test-all lint fmt render verify-cases doctor clean

# One-time: install deps, put `qwenbench` on PATH (editable, so config edits apply), create .env
setup:
	uv sync
	uv tool install --editable . --force
	uv run qwenbench setup

test:            ## unit + GPU-free end-to-end tests (never spends money)
	uv run pytest

test-all: test   ## plus read-only Runpod API checks (needs QWEN_IT_REAL_KEY)
	QWEN_RUNPOD_INTEGRATION=readonly uv run pytest tests/test_integration_runpod.py

lint:
	uv run ruff check src tests

fmt:
	uv run ruff check --fix src tests

render:          ## regenerate infra/runpod/rendered/*.json from config/
	uv run qwenbench infra render

verify-cases:    ## prove each benchmark case: base fails, reference passes
	uv run qwenbench bench verify-cases

doctor:
	uv run qwenbench doctor

clean:
	rm -rf .pytest_cache .ruff_cache
