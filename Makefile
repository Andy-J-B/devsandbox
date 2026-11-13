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

integration:
	pytest -q -m "integration and not docker"

check:
	./scripts/check.sh

security:
	DEVSANDBOX_E2E=1 python3 -m pytest -q -m security

e2e:
	./scripts/e2e.sh --deselect=tests/e2e/test_e2e_crash_soak.py::test_soak_random_crash_points

soak:
	./scripts/e2e.sh tests/e2e/test_e2e_crash_soak.py::test_soak_random_crash_points

check-all:
	./scripts/check-all.sh

drills:
	./scripts/drills.sh
