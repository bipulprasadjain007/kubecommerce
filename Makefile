# =============================================================================
# KubeCommerce - stable developer interface.
#
#   make install        uv sync every lib/service + install git hooks
#   make lint           ruff check + format check per project
#   make typecheck      mypy per project (scripts/typecheck.sh)
#   make test           pytest per project (unit; integration deselected)
#   make integration-test  compose up + cross-service tests (Phase C/3)
#   make compose-up / compose-down
#   make build          buildx images tagged with the git SHA
#   make kind-up / kind-down
#   make helm-install   install the Helm chart into kubecommerce-dev
#   make smoke-test / load-test
#   make security-scan  trivy fs + config (if trivy is installed)
#   make clean
#
# Tools that are not installed yet produce a clear message instead of cryptic
# failures. Targets are idempotent.
# =============================================================================

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Load local developer settings (git-ignored) and export to every recipe.
-include .env
export

UV ?= uv
GIT_SHA ?= $(shell git rev-parse --short=12 HEAD 2>/dev/null || echo dev)
IMAGE_REGISTRY ?= ghcr.io/bipulprasadjain007
IMAGE_TAG ?= $(GIT_SHA)
ENV ?= dev
SERVICES := $(notdir $(wildcard services/*))

.PHONY: help install lint typecheck test integration-test compose-up compose-down \
        build kind-up kind-down k8s-secrets helm-install smoke-test load-test \
        security-scan clean

help: ## Show this help.
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Sync deps for every uv project and install pre-commit hooks.
	@set -euo pipefail; \
	found=0; \
	for d in libs/*/ services/*/; do \
	  if [ -f "$${d}pyproject.toml" ]; then \
	    found=1; \
	    echo "==> uv sync $${d}"; \
	    ( cd "$$d" && $(UV) sync ); \
	  fi; \
	done; \
	[ "$$found" -eq 1 ] || echo "No uv projects found yet (libs/*, services/*)."; \
	if [ -d .git ]; then \
	  if command -v pre-commit >/dev/null 2>&1; then \
	    pre-commit install || echo "pre-commit install failed (non-fatal); run it manually."; \
	  else \
	    echo "pre-commit not found; install with 'uv tool install pre-commit' then run 'pre-commit install'."; \
	  fi; \
	else \
	  echo "No .git directory; skipping pre-commit install."; \
	fi

lint: ## Ruff check + format check for every uv project.
	@set -euo pipefail; \
	found=0; \
	for d in libs/*/ services/*/; do \
	  if [ -f "$${d}pyproject.toml" ]; then \
	    found=1; \
	    echo "==> ruff $${d}"; \
	    ( cd "$$d" && $(UV) run ruff check . && $(UV) run ruff format --check . ); \
	  fi; \
	done; \
	[ "$$found" -eq 1 ] || echo "No uv projects found yet; nothing to lint."

typecheck: ## Run mypy per project (scripts/typecheck.sh).
	@UV="$(UV)" bash scripts/typecheck.sh

test: ## Run unit tests for every uv project (deselect integration).
	@set -euo pipefail; \
	found=0; \
	for d in libs/*/ services/*/; do \
	  if [ -f "$${d}pyproject.toml" ]; then \
	    found=1; \
	    echo "==> pytest $${d}"; \
	    ( cd "$$d" && $(UV) run pytest -q -m "not integration" ); \
	  fi; \
	done; \
	[ "$$found" -eq 1 ] || echo "No uv projects found yet; nothing to test."

integration-test: ## Compose up + cross-service tests (needs compose.yaml, Phase C).
	@set -euo pipefail; \
	if [ ! -f compose.yaml ]; then \
	  echo "ERROR: compose.yaml not found (added in Phase C)."; \
	  exit 1; \
	fi; \
	if [ ! -d tests/integration ]; then \
	  echo "ERROR: tests/integration not found (added in Phase C)."; \
	  exit 1; \
	fi; \
	if ! command -v docker >/dev/null 2>&1; then \
	  echo "ERROR: docker is required for integration tests but was not found."; \
	  echo "       Install Docker Engine/Desktop, then re-run 'make integration-test'."; \
	  exit 1; \
	fi; \
	if [ ! -f .secrets/jwt-private.pem ]; then \
	  echo "ERROR: .secrets/jwt-private.pem not found."; \
	  echo "       Run 'bash scripts/dev-bootstrap.sh' first to generate JWT keys and .env."; \
	  exit 1; \
	fi; \
	if [ ! -f .env ]; then \
	  echo "WARNING: .env not found; compose defaults apply. Run 'bash scripts/dev-bootstrap.sh' to create it."; \
	fi; \
	trap 'docker compose down -v' EXIT; \
	docker compose up -d --build --wait --wait-timeout 300; \
	$(UV) run --project tests/integration pytest -q -m "integration"; \
	RUN_FAILURE_TESTS=1 $(UV) run --project tests/integration pytest -q -m "failure"

compose-up: ## Start the local Docker Compose stack and wait until healthy.
	@if ! command -v docker >/dev/null 2>&1; then \
	  echo "ERROR: docker is required but was not found. Install Docker Engine/Desktop."; \
	  exit 1; \
	fi; \
	if [ ! -f compose.yaml ]; then \
	  echo "ERROR: compose.yaml not found (added in Phase C)."; \
	  exit 1; \
	fi; \
	if [ ! -f .secrets/jwt-private.pem ]; then \
	  echo "ERROR: .secrets/jwt-private.pem not found."; \
	  echo "       Run 'bash scripts/dev-bootstrap.sh' first to generate JWT keys and .env."; \
	  exit 1; \
	fi; \
	if [ ! -f .env ]; then \
	  echo "WARNING: .env not found; compose defaults apply. Run 'bash scripts/dev-bootstrap.sh' to create it."; \
	fi; \
	docker compose up -d --build --wait --wait-timeout 300

compose-down: ## Stop the local Docker Compose stack and remove volumes.
	@if ! command -v docker >/dev/null 2>&1; then \
	  echo "ERROR: docker is required but was not found."; \
	  exit 1; \
	fi; \
	if [ ! -f compose.yaml ]; then \
	  echo "ERROR: compose.yaml not found (added in Phase C)."; \
	  exit 1; \
	fi; \
	docker compose down -v

build: ## Build all service images tagged with the git SHA (+ latest-dev locally).
	@if ! command -v docker >/dev/null 2>&1; then \
	  echo "ERROR: docker is required to build images but was not found."; \
	  exit 1; \
	fi; \
	built=0; \
	for svc in $(SERVICES); do \
	  if [ -f "services/$${svc}/Dockerfile" ]; then \
	    built=1; \
	    echo "==> building $${svc}"; \
	    docker buildx build \
	      --platform linux/amd64 \
	      -f "services/$${svc}/Dockerfile" \
	      -t "$(IMAGE_REGISTRY)/kubecommerce-$${svc}:$(GIT_SHA)" \
	      -t "$(IMAGE_REGISTRY)/kubecommerce-$${svc}:latest-dev" \
	      --load .; \
	  fi; \
	done; \
	[ "$$built" -eq 1 ] || echo "No service Dockerfiles found under services/*/ (Phase 3)."

kind-up: ## Create the local kind cluster (scripts/kind-up.sh, Phase 4).
	@if [ ! -f scripts/kind-up.sh ]; then \
	  echo "scripts/kind-up.sh not found (added in Phase 4). Nothing to do."; \
	  exit 0; \
	fi; \
	bash scripts/kind-up.sh

kind-down: ## Delete the local kind cluster (scripts/kind-down.sh).
	@if [ ! -f scripts/kind-down.sh ]; then \
	  echo "scripts/kind-down.sh not found (added in Phase 4). Nothing to do."; \
	  exit 0; \
	fi; \
	bash scripts/kind-down.sh

k8s-secrets: ## Create/refresh local kind Kubernetes secrets (ENV=dev).
	bash scripts/k8s-dev-secrets.sh $(ENV)

helm-install: ## Install/upgrade the chart into kubecommerce-dev (Phase 5/GitOps).
	@if ! command -v helm >/dev/null 2>&1; then \
	  echo "ERROR: helm is not installed. See docs/runbook.md for install steps."; \
	  exit 1; \
	fi; \
	chart="kubecommerce-gitops/charts/kubecommerce"; \
	values="kubecommerce-gitops/environments/dev/values.yaml"; \
	if [ ! -d "$$chart" ]; then \
	  echo "ERROR: Helm chart not found at $$chart (added in Phase 5/GitOps)."; \
	  exit 1; \
	fi; \
	helm upgrade --install kubecommerce "$$chart" \
	  -n kubecommerce-dev --create-namespace \
	  -f "$$values"

smoke-test: ## Run scripts/smoke-test.sh against the running stack.
	@bash scripts/smoke-test.sh

load-test: ## Run the k6 order-flow load test (scripts/load-test.sh).
	@bash scripts/load-test.sh

security-scan: ## Trivy filesystem + config scans (if trivy is installed).
	@if command -v trivy >/dev/null 2>&1; then \
	  echo "==> trivy fs (vuln/secret/misconfig)"; \
	  trivy fs --scanners vuln,secret,misconfig --exit-code 1 --severity CRITICAL,HIGH .; \
	  echo "==> trivy config"; \
	  trivy config --exit-code 1 .; \
	else \
	  echo "trivy not installed; skipping security scan. See docs/security.md."; \
	fi

clean: ## Remove caches, virtualenvs, and build artifacts.
	@find . -type d \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache -o -name .mypy_cache \) -prune -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name .venv -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf artifacts dist build htmlcov .coverage coverage.xml 2>/dev/null || true
	@echo "Cleaned caches, virtualenvs, and build artifacts."
