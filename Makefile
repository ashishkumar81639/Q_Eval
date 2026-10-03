PG_URL := postgresql+psycopg://qeval:qeval@localhost:5433/qeval
K ?= 5

.PHONY: install up down down-force eval eval-lite test lint smoke validate

install:
	uv sync

up:
	docker compose up -d --wait db
	@if [ -f runs/latest.dump ] && [ "$$(docker compose exec -T db psql -U qeval -tAc "select count(*) from information_schema.tables where table_name='runs'")" = "0" ]; then \
		echo "restoring runs/latest.dump"; \
		docker compose exec -T db pg_restore -U qeval -d qeval --no-owner < runs/latest.dump; \
	fi

# The volume is deleted only after a successful dump; use down-force to discard data deliberately.
down:
	DATABASE_URL=$(PG_URL) uv run qeval dump
	docker compose down -v

down-force:
	docker compose down -v

eval: install
	@if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then \
		$(MAKE) up && DATABASE_URL=$(PG_URL) uv run qeval eval scenarios/ --k $(K); status=$$?; $(MAKE) down; exit $$status; \
	else \
		uv run qeval eval scenarios/ --k $(K); \
	fi

eval-lite: install
	uv run qeval eval scenarios/ --k $(K)

test:
	uv run pytest tests/unit

validate:
	uv run qeval validate

lint:
	uv run ruff check . && uv run ruff format --check . && uv run mypy qeval

smoke:
	QEVAL_LIVE=1 uv run pytest tests/live -k smoke -v
