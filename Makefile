# Development shortcuts. Everything here also works as a plain command; the
# Makefile just records the canonical form.

.DEFAULT_GOAL := help
PYTHON ?= python

.PHONY: help install demo eval eval-review test lint format typecheck check serve docker clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Create the environment and install the package with dev extras
	$(PYTHON) -m venv .venv
	./.venv/bin/pip install -e ".[dev]"

demo: ## Run the full self-healing demo (offline, no API key needed)
	sentinel demo --rules-only

eval: ## Score the diagnosis engine against the golden incident set
	sentinel eval --rules-only

eval-review: ## Check that no harmful action gets past the reviewer and the hard rules
	sentinel eval-review --rules-only

test: ## Run the test suite with coverage
	pytest --cov=sentinel --cov-report=term-missing

lint: ## Lint and check formatting
	ruff check src tests
	ruff format --check src tests

format: ## Apply formatting and safe lint fixes
	ruff format src tests
	ruff check --fix src tests

typecheck: ## Static type check
	mypy

check: lint typecheck test ## Everything CI runs, locally

serve: ## Serve the incident console on http://127.0.0.1:8000
	sentinel serve

docker: ## Build the container image
	docker build -t sentinel:local .

clean: ## Remove caches and the local warehouse
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml data
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
