# DevSandbox database

- SQLAlchemy 2.x async models in `src/devsandbox/db/models.py`
- Alembic migration `0001_initial` in `src/devsandbox/db/migrations/versions/`
- Repositories in `src/devsandbox/db/repositories/`
- Transaction helper: `devsandbox.db.session.unit_of_work`

`TaskRepo.transition` is the single write path for task status changes: it
performs a compare-and-swap (`WHERE id = :id AND status = ANY(:from)`) in a single
transaction, bumps the optimistic-lock `version`, and writes the audit event.
