.PHONY: run dev install stop redis clean test lint format backup

# Auto-detect Python 3.11 (brew on macOS), fallback to python3
PYTHON := $(shell command -v python3.11 2>/dev/null || command -v python3 2>/dev/null)
VENV := venv
ACTIVATE := . $(VENV)/bin/activate

# === Main command: one command to launch everything ===
run: $(VENV) redis
	@echo "Starting Yaad on http://localhost:8000 ..."
	@$(ACTIVATE) && uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Same as run (alias)
dev: run

# === Setup ===

# Create venv + install deps if venv doesn't exist
$(VENV): pyproject.toml
	@if [ ! -d "$(VENV)" ]; then \
		echo "Creating venv with $(PYTHON)..."; \
		$(PYTHON) -m venv $(VENV); \
		$(ACTIVATE) && pip install -e .; \
		echo "Done."; \
	fi

# Force reinstall dependencies
install:
	@rm -rf $(VENV)
	$(PYTHON) -m venv $(VENV)
	$(ACTIVATE) && pip install -e ".[dev]"

# === Services ===

# Start Redis (local, skip if already running or unavailable)
redis:
	@if command -v redis-cli >/dev/null 2>&1; then \
		redis-cli ping >/dev/null 2>&1 || (echo "Starting Redis..." && brew services start redis 2>/dev/null || redis-server --daemonize yes 2>/dev/null || true); \
	elif command -v docker >/dev/null 2>&1; then \
		docker start yaad-redis 2>/dev/null || docker run -d --name yaad-redis -p 6379:6379 redis:7-alpine 2>/dev/null || true; \
	else \
		echo "Warning: Redis not found. Install with: brew install redis"; \
	fi

# Stop everything
stop:
	@kill $$(lsof -ti:8000) 2>/dev/null || true
	@brew services stop redis 2>/dev/null || docker stop yaad-redis 2>/dev/null || true
	@echo "Stopped."

# === Dev tools ===

test:
	@$(ACTIVATE) && pytest tests/ -v --tb=short

lint:
	@$(ACTIVATE) && ruff check src/ tests/

format:
	@$(ACTIVATE) && ruff format src/ tests/ && ruff check --fix src/ tests/

# Database migrations
db:
	@$(ACTIVATE) && alembic upgrade head

# Backup (Neon.tech cloud DB)
backup:
	@$(ACTIVATE) && pg_dump "$(DATABASE_URL)" > yaad_backup_$$(date +%Y%m%d_%H%M%S).sql
	@echo "Backup done."

# Clean caches
clean:
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@find . -type f -name "*.pyc" -delete 2>/dev/null || true
