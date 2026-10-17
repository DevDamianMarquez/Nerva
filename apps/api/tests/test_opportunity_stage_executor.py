"""Milestone 5 Block 3: `UpdateOpportunityStageExecutor`, the first real `ActionExecutor`.

Exercises the full, real lifecycle — `ActionService`, `OpportunityService`,
`ActionExecutionEngine` and this executor, all against the real (database-backed) test
session — with no fake standing in for anything: there is no external provider here to
fake. See `test_action_execution_engine.py` for the equivalent tests against
`FakeActionExecutor`, which this file deliberately mirrors in structure.
"""

import socket
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionStatus, ActionType, ActorType, MemberRole, OpportunityStage
from nerva.core.errors import NotFoundError
from nerva.models import Action, Opportunity, Workspace
from nerva.nerva_actions.engine import ActionExecutionEngine
from nerva.nerva_actions.errors import ActionNotExecutableError
from nerva.nerva_actions.opportunity_stage_executor import UpdateOpportunityStageExecutor
from nerva.schemas.actions import ActionCreate
from nerva.schemas.crm import OpportunityCreate
from nerva.services.actions import ActionService
from nerva.services.activity import ActivityService
from nerva.services.crm import OpportunityService
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


@pytest.fixture
def setup(make_user: MakeUser, make_workspace: MakeWorkspace) -> tuple[Account, uuid.UUID]:
    owner = make_user("Alice")
    return owner, uuid.UUID(make_workspace(owner, "Northwind"))


@pytest.fixture
def ctx(setup: tuple[Account, uuid.UUID]) -> WorkspaceContext:
    owner, workspace_id = setup
    return WorkspaceContext(
        workspace_id=workspace_id, user_id=uuid.UUID(owner.id), role=MemberRole.OWNER
    )


@pytest.fixture
def actions(db_session: Session, ctx: WorkspaceContext) -> ActionService:
    return ActionService(db_session, ctx)


@pytest.fixture
def opportunities(db_session: Session, ctx: WorkspaceContext) -> OpportunityService:
    return OpportunityService(db_session, ctx)


@pytest.fixture
def activity(db_session: Session, ctx: WorkspaceContext) -> ActivityService:
    return ActivityService(db_session, ctx.workspace_id)


@pytest.fixture
def executor(db_session: Session, ctx: WorkspaceContext) -> UpdateOpportunityStageExecutor:
    return UpdateOpportunityStageExecutor(db_session, ctx)


@pytest.fixture
def execution_engine(
    db_session: Session, ctx: WorkspaceContext, executor: UpdateOpportunityStageExecutor
) -> ActionExecutionEngine:
    return ActionExecutionEngine(db_session, ctx, {ActionType.UPDATE_OPPORTUNITY_STAGE: executor})


def _make_opportunity(
    opportunities: OpportunityService, *, stage: OpportunityStage = OpportunityStage.NEW
) -> Opportunity:
    return opportunities.create(OpportunityCreate(title="Halcyon Freight: 3 seats", stage=stage))


def _propose(
    actions: ActionService,
    opportunity_id: uuid.UUID | None,
    *,
    payload: dict[str, object] | None = None,
) -> Action:
    return actions.propose(
        ActionCreate.model_validate(
            {
                "type": "update_opportunity_stage",
                "summary": "Move to qualified",
                "payload": payload if payload is not None else {"target_stage": "qualified"},
                "opportunity_id": opportunity_id,
            }
        )
    )


def _approved(
    actions: ActionService,
    opportunity_id: uuid.UUID | None,
    *,
    payload: dict[str, object] | None = None,
) -> Action:
    action = _propose(actions, opportunity_id, payload=payload)
    return actions.approve(action.id)


# ---------------------------------------------------------------------------
# A. Happy path
# ---------------------------------------------------------------------------


def test_a_successful_execution_moves_the_opportunity_to_the_target_stage(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.COMPLETED
    assert opportunities.get(opportunity.id).stage is OpportunityStage.QUALIFIED


def test_a_successful_execution_persists_a_safe_result(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
) -> None:
    opportunity = _make_opportunity(opportunities, stage=OpportunityStage.NEW)
    action = _approved(actions, opportunity.id)

    result = execution_engine.execute(action.id)

    assert result.result == {
        "opportunity_id": str(opportunity.id),
        "previous_stage": "new",
        "new_stage": "qualified",
    }


# ---------------------------------------------------------------------------
# B/C. Approval requirement and lifecycle state
# ---------------------------------------------------------------------------


def test_a_proposed_action_cannot_execute(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _propose(actions, opportunity.id)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert opportunities.get(opportunity.id).stage is OpportunityStage.NEW


def test_a_rejected_action_cannot_execute(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _propose(actions, opportunity.id)
    actions.reject(action.id)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert opportunities.get(opportunity.id).stage is OpportunityStage.NEW


def test_a_completed_action_cannot_execute_again(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id)
    execution_engine.execute(action.id)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    # Still at the stage the single successful execution moved it to.
    assert opportunities.get(opportunity.id).stage is OpportunityStage.QUALIFIED


# ---------------------------------------------------------------------------
# D. Idempotency
# ---------------------------------------------------------------------------


def test_executing_the_same_action_twice_never_reapplies_the_mutation(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
    activity: ActivityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id)

    execution_engine.execute(action.id)
    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)

    assert opportunities.get(opportunity.id).stage is OpportunityStage.QUALIFIED
    completed_events = [
        e
        for e in activity.list(entity_type="action", entity_id=action.id)
        if e.event_type == "action.completed"
    ]
    assert len(completed_events) == 1


# ---------------------------------------------------------------------------
# E. Tenant isolation
# ---------------------------------------------------------------------------


def test_an_action_from_another_workspace_cannot_update_the_opportunity(
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    actions: ActionService,
    opportunities: OpportunityService,
    executor: UpdateOpportunityStageExecutor,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id)

    other_owner = make_user("Bob")
    other_workspace_id = uuid.UUID(make_workspace(other_owner, "Acme"))
    other_ctx = WorkspaceContext(
        workspace_id=other_workspace_id, user_id=uuid.UUID(other_owner.id), role=MemberRole.OWNER
    )
    other_engine = ActionExecutionEngine(
        db_session, other_ctx, {ActionType.UPDATE_OPPORTUNITY_STAGE: executor}
    )

    with pytest.raises(NotFoundError):
        other_engine.execute(action.id)
    assert opportunities.get(opportunity.id).stage is OpportunityStage.NEW


def test_an_action_cannot_reference_an_opportunity_from_another_workspace_at_the_database_level(
    db_session: Session,
) -> None:
    """The guarantee the executor leans on, proven directly: `tenant_fk` makes it
    impossible to even construct this cross-tenant reference, regardless of what any
    executor does."""
    first = Workspace(name="One", slug="one")
    second = Workspace(name="Two", slug="two")
    db_session.add_all([first, second])
    db_session.flush()
    opportunity = Opportunity(
        workspace_id=first.id, title="Cross-tenant", created_by_type=ActorType.NERVA
    )
    db_session.add(opportunity)
    db_session.flush()

    db_session.add(
        Action(
            workspace_id=second.id,
            type=ActionType.UPDATE_OPPORTUNITY_STAGE.value,
            summary="Move to qualified",
            payload={"target_stage": "qualified"},
            opportunity_id=opportunity.id,
            created_by_type=ActorType.NERVA,
        )
    )

    with pytest.raises(IntegrityError, match="fk_actions"):
        db_session.flush()


# ---------------------------------------------------------------------------
# F. Invalid payload
# ---------------------------------------------------------------------------


def test_a_malformed_payload_fails_before_the_opportunity_is_touched(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id, payload={"target_stage": "not-a-stage"})

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert opportunities.get(opportunity.id).stage is OpportunityStage.NEW


def test_an_approved_action_without_an_opportunity_id_cannot_execute(
    execution_engine: ActionExecutionEngine, actions: ActionService
) -> None:
    action = _approved(actions, None)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert actions.get(action.id).status is ActionStatus.APPROVED


# ---------------------------------------------------------------------------
# G. Audit: no duplicate events from the executor
# ---------------------------------------------------------------------------


def test_a_successful_execution_writes_exactly_the_action_lifecycle_events(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
    activity: ActivityService,
) -> None:
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id)

    execution_engine.execute(action.id)

    events = [e.event_type for e in activity.list(entity_type="action", entity_id=action.id)]
    # Newest first: completed, executing, approved, proposed — exactly four, none of
    # them written by the executor itself (it has no access to `ActivityService`).
    assert events == ["action.completed", "action.executing", "action.approved", "action.proposed"]


# ---------------------------------------------------------------------------
# H. Executor isolation: no network calls
# ---------------------------------------------------------------------------


def test_the_executor_makes_no_network_calls(
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    opportunities: OpportunityService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("the internal executor must never open a socket")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    opportunity = _make_opportunity(opportunities)
    action = _approved(actions, opportunity.id)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.COMPLETED
