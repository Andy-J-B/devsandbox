COMPOSE ?= docker compose -f docker/compose.yaml

up:
	$(COMPOSE) up -d postgres

down:
	$(COMPOSE) down

orchestrator:
	DEVSANDBOX_API_TOKEN=$${DEVSANDBOX_API_TOKEN:?set DEVSANDBOX_API_TOKEN} $(COMPOSE) up -d orchestrator

migrate:
	alembic upgrade head

test:
	pytest -q

check:
	./scripts/check.sh
