.DEFAULT_GOAL := help
.PHONY: help setup lint fmt typecheck test check samples archives clean

help: ## list targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

setup: ## install deps (workspace incl. lake/) and git hooks
	uv sync --all-packages --all-groups
	uv run pre-commit install --hook-type pre-commit --hook-type commit-msg

lint: ## ruff check + format check (no changes)
	uv run ruff check .
	uv run ruff format --check .

fmt: ## ruff autofix + format
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy strict
	uv run mypy

test: ## pytest with coverage (live tests deselected)
	uv run pytest --cov --cov-report=term-missing

check: lint typecheck test ## everything CI runs for Python

samples: ## trimmed API sample per product -> samples/api/ (needs ERCOT_*; PRODUCTS=a,b optional)
	uv run python -m scripts.fetch_samples $(if $(PRODUCTS),--products $(PRODUCTS),)

archives: ## trimmed latest posting per product -> samples/archive/ (needs ERCOT_*; PRODUCTS=a,b)
	uv run python -m scripts.fetch_archives $(if $(PRODUCTS),--products $(PRODUCTS),)

clean: ## remove caches
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
