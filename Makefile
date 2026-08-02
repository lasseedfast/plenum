.PHONY: help setup dev backend frontend build test lint schema mcp check-fork-divergence

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

setup: ## Create the venv and install Python + frontend dependencies
	python3 -m venv .venv
	.venv/bin/pip install -e ".[dev]"
	cd frontend && npm install
	@test -f .env || (cp .env.example .env && echo "Created .env — fill it in before running.")

dev: ## Run the API with reload (frontend: `cd frontend && npm run dev`)
	.venv/bin/uvicorn backend.app:app --reload --port 8000

backend: dev

frontend: ## Build the production frontend bundle
	cd frontend && npm install && npm run build

build: frontend ## Alias for frontend

schema: ## Apply schema.sql to the configured database
	@set -a; . ./.env; set +a; \
	psql -h "$$PG_HOST" -U "$$PG_USER" -d "$$PG_DB" -v ON_ERROR_STOP=1 -f _postgres/schema.sql

mcp: ## Run the MCP server (optional; needs the `mcp` extra)
	.venv/bin/python riksdagen_mcp/server.py

test: ## Run the test suite
	.venv/bin/pytest -q

lint: ## Lint Python and type-check the frontend
	.venv/bin/ruff check .
	cd frontend && npx tsc --noEmit

# For a production fork: everything deployment-specific belongs in deploy/prod/, a
# path upstream never writes to. If this reports anything else, the layering has
# sprung a leak and the next upstream merge will conflict.
check-fork-divergence: ## Verify the fork differs from upstream only under deploy/prod/
	@git fetch upstream --quiet 2>/dev/null || { echo "No 'upstream' remote configured."; exit 1; }
	@diff=$$(git diff --stat upstream/main..HEAD -- . ':!deploy/prod'); \
	if [ -n "$$diff" ]; then \
		echo "$$diff"; \
		echo "Fork diverges from upstream outside deploy/prod/."; exit 1; \
	else \
		echo "Fork is clean: no differences outside deploy/prod/."; \
	fi
