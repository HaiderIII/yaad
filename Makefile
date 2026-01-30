.PHONY: start stop dev services db css install clean backup

# Start all services and run the app
start: services dev

# Start Docker services (PostgreSQL + Redis)
services:
	@echo "Stopping local PostgreSQL if running..."
	@sudo service postgresql stop 2>/dev/null || true
	@echo "Starting Docker containers..."
	@sudo docker start yaad-postgres yaad-redis 2>/dev/null || sudo docker compose up -d postgres redis
	@echo "Services started."

# Stop Docker services
stop:
	@echo "Stopping Docker containers..."
	@sudo docker stop yaad-postgres yaad-redis 2>/dev/null || true
	@echo "Services stopped."

# Run development server
dev:
	@echo "Starting Yaad server on http://localhost:8000 ..."
	. venv/bin/activate && uvicorn src.main:app --reload --host 0.0.0.0 --port 8000

# Run database migrations
db:
	. venv/bin/activate && alembic upgrade head

# Watch CSS changes (run in separate terminal)
css:
	npm run watch

# Backup database
backup:
	@echo "Backing up database..."
	@sudo docker exec yaad-postgres pg_dump -h localhost -U yaad yaad > yaad_backup_$$(date +%Y%m%d_%H%M%S).sql
	@echo "Backup done."

# Install dependencies
install:
	python -m venv venv
	. venv/bin/activate && pip install -r requirements.txt
	npm install

# Clean up
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
