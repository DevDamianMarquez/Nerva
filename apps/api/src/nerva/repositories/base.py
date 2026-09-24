"""Base class for repositories of workspace-owned data."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from nerva.db.base import WorkspaceOwnedModel


class ScopedRepository[ModelT: WorkspaceOwnedModel]:
    """A repository bound to one workspace for its whole lifetime.

    Every read is filtered by the bound `workspace_id`, and every insert stamps it,
    so callers cannot query or write across tenants by accident. The bound id must
    come from an authorized `WorkspaceContext`, never from request input.
    """

    model: type[ModelT]

    def __init__(self, session: Session, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _select(self) -> Select[tuple[ModelT]]:
        return select(self.model).where(self.model.workspace_id == self.workspace_id)

    def get(self, entity_id: uuid.UUID) -> ModelT | None:
        return self.session.scalars(self._select().where(self.model.id == entity_id)).one_or_none()

    def add(self, entity: ModelT) -> ModelT:
        entity.workspace_id = self.workspace_id
        self.session.add(entity)
        self.session.flush()
        return entity

    def _page(
        self, statement: Select[tuple[ModelT]], *, limit: int, offset: int
    ) -> Sequence[ModelT]:
        ordered = statement.order_by(self.model.created_at.desc(), self.model.id)
        return self.session.scalars(ordered.limit(limit).offset(offset)).all()

    def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[ModelT]:
        return self._page(self._select(), limit=limit, offset=offset)
