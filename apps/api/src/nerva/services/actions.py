"""Proposing, deciding on and (in later versions) executing actions."""

import uuid
from collections.abc import Callable, Sequence
from typing import Any

from nerva.core.context import Actor
from nerva.core.enums import ActionStatus, ActivityEventType
from nerva.core.errors import NotFoundError
from nerva.db.base import utcnow
from nerva.models import Action
from nerva.nerva_actions.lifecycle import ensure_transition
from nerva.repositories.workspace_data import ActionRepository
from nerva.schemas.actions import ActionCreate
from nerva.services.base import ScopedService


class ActionService(ScopedService):
    """Every status change goes through `_transition`, which enforces the lifecycle,
    locks the row against concurrent decisions, and records an activity event, all in
    one transaction.

    `approve` and `reject` are called by humans through the API. `start_execution`,
    `complete` and `fail` are for Nerva's executor and are intentionally not exposed
    over HTTP: a client must not be able to mark an action as done.
    """

    @property
    def _repo(self) -> ActionRepository:
        return ActionRepository(self.session, self.ctx.workspace_id)

    def propose(
        self,
        data: ActionCreate,
        *,
        actor: Actor | None = None,
        proposal_fingerprint: str | None = None,
        provenance: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> Action:
        """`proposal_fingerprint`/`provenance`/`commit` exist for M7.1c's
        `ProposalMaterializationService` and default to exactly the prior
        behavior for every existing caller (a human, via `POST /actions`,
        which never passes any of the three): `proposal_fingerprint=None`
        (a human-proposed action never has one — see `nerva.models.
        actions.Action`'s own docstring), `provenance=None` (the
        `ACTION_PROPOSED` event's `data` stays exactly `{"action_type":
        ...}`, unchanged), `commit=True` (commits immediately, as before).

        `provenance`, when given, is merged into the *same* `ACTION_PROPOSED`
        `ActivityEvent` this method already writes — never a second event —
        so an automatic proposal's reasoning-state identity is auditable
        without any new event type or a second write. Internal ids are
        expected and correct inside `provenance` (e.g. `conversation_id`,
        `analysis_id`): the "no internal ids" rule is about what an LLM is
        ever shown, not about this workspace's own audit trail — see
        `nerva.services.proposal_materialization`'s module docstring.

        `commit=False` lets a caller (again, only `ProposalMaterializationService`
        today) wrap this call in its own `Session.begin_nested()` SAVEPOINT,
        so a unique-constraint violation on `proposal_fingerprint` can be
        rolled back to that savepoint alone — never the caller's whole
        transaction — the same reasoning `MemoryService.replace` already
        documents for its own `commit=False` composition."""
        actor = actor or self.ctx.actor
        self._require_opportunity(data.opportunity_id)
        action = self._repo.add(
            Action(
                type=data.type.value,
                status=ActionStatus.PROPOSED,
                summary=data.summary,
                payload=dict(data.payload),
                opportunity_id=data.opportunity_id,
                proposal_fingerprint=proposal_fingerprint,
                created_by_type=actor.type,
                created_by_user_id=actor.user_id,
            )
        )
        event_data: dict[str, Any] = {"action_type": action.type}
        if provenance:
            event_data.update(provenance)
        self.activity.record(
            actor=actor,
            event_type=ActivityEventType.ACTION_PROPOSED,
            entity_type="action",
            entity_id=action.id,
            summary=f"Proposed: {action.summary}",
            data=event_data,
        )
        if commit:
            self.session.commit()
        return action

    def get(self, action_id: uuid.UUID) -> Action:
        action = self._repo.get(action_id)
        if action is None:
            raise NotFoundError("Action not found.")
        return action

    def list(self, *, status: ActionStatus | None, limit: int, offset: int) -> Sequence[Action]:
        return self._repo.list_filtered(status=status, limit=limit, offset=offset)

    def approve(self, action_id: uuid.UUID) -> Action:
        def apply(action: Action) -> None:
            action.approved_by_user_id = self.ctx.user_id
            action.approved_at = utcnow()

        return self._transition(
            action_id,
            ActionStatus.APPROVED,
            actor=self.ctx.actor,
            event_type=ActivityEventType.ACTION_APPROVED,
            summary_prefix="Approved",
            apply=apply,
        )

    def reject(self, action_id: uuid.UUID, *, reason: str | None = None) -> Action:
        def apply(action: Action) -> None:
            action.rejected_by_user_id = self.ctx.user_id
            action.rejected_at = utcnow()
            action.rejection_reason = reason

        return self._transition(
            action_id,
            ActionStatus.REJECTED,
            actor=self.ctx.actor,
            event_type=ActivityEventType.ACTION_REJECTED,
            summary_prefix="Rejected",
            apply=apply,
            extra={"reason": reason} if reason else None,
        )

    def start_execution(self, action_id: uuid.UUID) -> Action:
        def apply(action: Action) -> None:
            action.started_at = utcnow()

        return self._transition(
            action_id,
            ActionStatus.EXECUTING,
            actor=Actor.nerva(),
            event_type=ActivityEventType.ACTION_EXECUTING,
            summary_prefix="Started",
            apply=apply,
        )

    def complete(self, action_id: uuid.UUID, *, result: dict[str, Any] | None = None) -> Action:
        def apply(action: Action) -> None:
            action.finished_at = utcnow()
            action.result = result or {}

        return self._transition(
            action_id,
            ActionStatus.COMPLETED,
            actor=Actor.nerva(),
            event_type=ActivityEventType.ACTION_COMPLETED,
            summary_prefix="Completed",
            apply=apply,
        )

    def fail(self, action_id: uuid.UUID, *, error: str) -> Action:
        def apply(action: Action) -> None:
            action.finished_at = utcnow()
            action.result = {"error": error}

        return self._transition(
            action_id,
            ActionStatus.FAILED,
            actor=Actor.nerva(),
            event_type=ActivityEventType.ACTION_FAILED,
            summary_prefix="Failed",
            apply=apply,
            extra={"error": error},
        )

    def _transition(
        self,
        action_id: uuid.UUID,
        target: ActionStatus,
        *,
        actor: Actor,
        event_type: ActivityEventType,
        summary_prefix: str,
        apply: Callable[[Action], None],
        extra: dict[str, Any] | None = None,
    ) -> Action:
        action = self._repo.get_for_update(action_id)
        if action is None:
            raise NotFoundError("Action not found.")
        previous = action.status
        ensure_transition(previous, target)  # raises InvalidStateTransitionError -> 409
        action.status = target
        apply(action)
        self.activity.record(
            actor=actor,
            event_type=event_type,
            entity_type="action",
            entity_id=action.id,
            summary=f"{summary_prefix}: {action.summary}",
            data={"from_status": previous.value, "to_status": target.value, **(extra or {})},
        )
        self.session.commit()
        return action
