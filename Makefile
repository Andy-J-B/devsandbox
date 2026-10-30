up:
	docker compose up -d postgres

down:
	docker compose down

migrate:
	alembic upgrade head

test:
	pytest -q

check:
	./scripts/check.sh
