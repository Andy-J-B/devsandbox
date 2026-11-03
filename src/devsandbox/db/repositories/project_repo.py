"""Project repository."""

from __future__ import annotations

import builtins
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ...common.errors import InvalidTransition, ProjectNotFoundError
from ...common.time import utcnow
from ...common.types import ProjectStatus
from ..models import EventModel, ProjectModel
from ._json import jsonable


class ProjectRepo:
    """Repository for project records.

    ``transition`` mirrors :meth:`TaskRepo.transition`: every project status
    change is a compare-and-swap in one transaction and writes an audit event.
    """

    def __init__(
        self,
        session: AsyncSession,
        is_legal: Callable[[ProjectStatus, ProjectStatus], bool] | None = None,
    ) -> None:
        self.session = session
        self._is_legal = is_legal

    def _legal(self, from_state: ProjectStatus, to_state: ProjectStatus) -> bool:
        if self._is_legal is None:
            return True
        return bool(self._is_legal(from_state, to_state))

    async def get(self, project_id: str) -> ProjectModel | None:
        """Return the project or ``None``."""
        result = await self.session.execute(
            select(ProjectModel).where(ProjectModel.id == project_id)
        )
        return result.scalar_one_or_none()

    async def create(self, project: ProjectModel) -> ProjectModel:
        """Persist a new project."""
        self.session.add(project)
        await self.session.flush()
        return project

    async def list(self) -> list[ProjectModel]:
        """Return all projects."""
        result = await self.session.execute(select(ProjectModel).order_by(ProjectModel.created_at))
        return list(result.scalars().all())

    async def list_by_status(
        self, statuses: Sequence[ProjectStatus]
    ) -> builtins.list[ProjectModel]:
        """Return projects in any of ``statuses``."""
        result = await self.session.execute(
            select(ProjectModel)
            .where(ProjectModel.status.in_([status.value for status in statuses]))
            .order_by(ProjectModel.created_at, ProjectModel.id)
        )
        return list(result.scalars().all())

    async def transition(
        self,
        project_id: str,
        from_states: Sequence[ProjectStatus],
        to_state: ProjectStatus,
        reason: str,
        **fields: Any,
    ) -> ProjectModel:
        """Atomically transition project status (CAS) and audit the event."""
        project = await self.get(project_id)
        if project is None:
            raise ProjectNotFoundError(f"project {project_id} not found")
        current = ProjectStatus(project.status)
        if current not in from_states:
            raise InvalidTransition(
                f"project {project_id} is in state {current.value}, expected one of "
                f"{[state.value for state in from_states]}"
            )
        if not self._legal(current, to_state):
            raise InvalidTransition(
                f"illegal project transition {current.value} -> {to_state.value}"
            )
        values: dict[str, Any] = {
            "status": to_state.value,
            "updated_at": utcnow(),
        }
        values.update(fields)
        stmt = (
            update(ProjectModel)
            .where(ProjectModel.id == project_id, ProjectModel.status == current.value)
            .values(**values)
            .returning(ProjectModel.id)
        )
        result = await self.session.execute(stmt)
        if result.scalar_one_or_none() is None:
            raise InvalidTransition(f"cas conflict transitioning project {project_id}")
        for key, value in values.items():
            setattr(project, key, value)
        self.session.add(
            EventModel(
                project_id=project_id,
                type="project.transition",
                from_state=current.value,
                to_state=to_state.value,
                payload_json=jsonable({"reason": reason, **fields}),
                at=utcnow(),
            )
        )
        await self.session.flush()
        return project
