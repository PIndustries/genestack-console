# Genestack Console — developer targets
# Config lives in config.yaml (not env vars). Optional: CONSOLE_CONFIG=...

PYTHON      ?= python3
VENV        ?= .venv
PIP         := $(VENV)/bin/pip
PYTEST      := $(VENV)/bin/pytest
UVICORN     := $(VENV)/bin/uvicorn
RUFF        := $(VENV)/bin/ruff

.PHONY: help install test run smoke lint clean venv

help:
	@echo "Genestack Console targets:"
	@echo "  make install  - create venv and install deps"
	@echo "  make test     - run pytest"
	@echo "  make run      - start API+UI on 127.0.0.1:8080 (config.yaml)"
	@echo "  make smoke    - curl /health and list operations"
	@echo "  make lint     - ruff check"
	@echo "  make clean    - remove venv, caches, local db"

venv:
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv
	@mkdir -p data
	@if [ -f requirements.txt ]; then $(PIP) install -r requirements.txt; fi
	@if [ -f requirements-dev.txt ]; then $(PIP) install -r requirements-dev.txt; fi
	-$(PIP) install ruff

test:
	@mkdir -p data
	@if [ -x $(PYTEST) ]; then \
		PATH="$(CURDIR)/$(VENV)/bin:$$PATH" $(PYTEST) -v --tb=short tests/; \
	else \
		PATH="$(CURDIR)/$(VENV)/bin:$$PATH" $(PYTHON) -m pytest -v --tb=short tests/; \
	fi

run:
	@mkdir -p data
	@# Default private bind (127.0.0.1). Expose via fleet VPN / reverse proxy outside this repo.
	@if [ -x $(UVICORN) ]; then \
		$(UVICORN) app.main:app --host 127.0.0.1 --port 8080 --reload; \
	else \
		$(PYTHON) -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload; \
	fi

smoke:
	@echo "==> GET /health"
	@curl -sf http://127.0.0.1:8080/health | tee /dev/stderr | grep -q . \
		&& echo " health ok" || (echo "health failed (is the server running? make run)" && exit 1)
	@echo "==> GET /api/v1/operations (admin)"
	@curl -sf -H "X-API-Key: dev-admin-key" http://127.0.0.1:8080/api/v1/operations \
		| head -c 500; echo
	@echo "smoke complete"

lint:
	@if [ -x $(RUFF) ]; then \
		$(RUFF) check app tests || true; \
	elif command -v ruff >/dev/null 2>&1; then \
		ruff check app tests || true; \
	else \
		echo "ruff not installed; skipping lint (make install to add it)"; \
	fi

clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache __pycache__ app/__pycache__ tests/__pycache__
	rm -rf data/*.db
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
