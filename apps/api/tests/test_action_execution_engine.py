"""Milestone 5 Block 2: `ActionExecutionEngine`.

Exercises `ActionExecutionEngine` against a real (database-backed) `ActionService` and
`FakeActionExecutor` — no network call, no real provider, no HTTP layer (none exists
yet for execution). See `test_action_execution_contracts.py` for the Block 1 contracts
the engine composes rather than duplicates.
"""

import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionStatus, ActionType, MemberRole
from nerva.core.errors import NotFoundError
from nerva.models import Action
from nerva.nerva_actions.engine import ActionExecutionEngine
from nerva.nerva_actions.errors import (
    ActionExecutionError,
    ActionExecutorUnavailableError,
    ActionNotExecutableError,
    ActionProviderError,
    ActionRateLimitError,
)
from nerva.nerva_actions.executor import ActionExecutionResult
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from nerva.services.activity import ActivityService
from tests.fakes.actions import FakeActionExecutor
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

VALID_SEND_EMAIL_PAYLOAD = {
    "recipients": ["priya@halcyon.example"],
    "subject": "Pricing for 3 seats",
    "body": "Hi Priya, following up on pricing for a 3-seat plan.",
}


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
def service(db_session: Session, ctx: WorkspaceContext) -> ActionService:
    return ActionService(db_session, ctx)


@pytest.fixture
def activity(db_session: Session, ctx: WorkspaceContext) -> ActivityService:
    return ActivityService(db_session, ctx.workspace_id)


@pytest.fixture
def executor() -> FakeActionExecutor:
    return FakeActionExecutor(ActionType.SEND_EMAIL)


@pytest.fixture
def execution_engine(
    db_session: Session, ctx: WorkspaceContext, executor: FakeActionExecutor
) -> ActionExecutionEngine:
    return ActionExecutionEngine(db_session, ctx, {ActionType.SEND_EMAIL: executor})


def _propose_send_email(
    service: ActionService, payload: dict[str, object] = VALID_SEND_EMAIL_PAYLOAD
) -> Action:
    return service.propose(
        ActionCreate.model_validate(
            {"type": "send_email", "summary": "Reply to Priya", "payload": payload}
        )
    )


def _approved(
    service: ActionService, payload: dict[str, object] = VALID_SEND_EMAIL_PAYLOAD
) -> Action:
    action = _propose_send_email(service, payload)
    return service.approve(action.id)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_a_successful_execution_calls_the_executor_exactly_once(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert executor.call_count == 1
    assert result.status is ActionStatus.COMPLETED


def test_a_successful_execution_transitions_through_executing_to_completed(
    execution_engine: ActionExecutionEngine, service: ActionService, activity: ActivityService
) -> None:
    action = _approved(service)

    execution_engine.execute(action.id)

    events = [e.event_type for e in activity.list(entity_type="action", entity_id=action.id)]
    # Newest first.
    assert events[:2] == ["action.completed", "action.executing"]


def test_a_successful_execution_persists_only_safe_result_metadata(
    execution_engine: ActionExecutionEngine,
    service: ActionService,
    executor: FakeActionExecutor,
) -> None:
    executor.next_result = ActionExecutionResult(
        external_reference_id="msg_123", metadata={"provider": "fake", "sent": True}
    )
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert result.result == {
        "external_reference_id": "msg_123",
        "provider": "fake",
        "sent": True,
    }


def test_a_successful_execution_passes_the_action_id_as_the_idempotency_key(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _approved(service)

    execution_engine.execute(action.id)

    assert executor.calls[0].idempotency_key == str(action.id)


# ---------------------------------------------------------------------------
# Executor failure
# ---------------------------------------------------------------------------


def test_an_executor_failure_calls_the_executor_once_and_ends_failed(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    executor.fail_with = ActionProviderError("raw provider response: <secret/>")
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert executor.call_count == 1
    assert result.status is ActionStatus.FAILED


def test_an_executor_failure_persists_only_a_safe_failure_message(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    executor.fail_with = ActionProviderError("raw provider response: token=abc123 <html>...</html>")
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert result.result is not None
    assert "token=abc123" not in result.result["error"]
    assert "<html>" not in result.result["error"]
    assert result.result == {"error": "Could not reach the provider."}


def test_a_rate_limit_failure_persists_a_safe_message_with_retry_after(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    executor.fail_with = ActionRateLimitError("provider: slow down", retry_after=30.0)
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result == {"error": "The provider is rate-limiting requests. Retry after 30s."}


def test_a_rate_limit_failure_without_retry_after_still_persists_a_safe_message(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    executor.fail_with = ActionRateLimitError("provider: slow down")
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert result.result == {"error": "The provider is rate-limiting requests."}


def test_an_executor_failure_transitions_through_executing_to_failed(
    execution_engine: ActionExecutionEngine,
    service: ActionService,
    executor: FakeActionExecutor,
    activity: ActivityService,
) -> None:
    executor.fail_with = ActionProviderError("boom")
    action = _approved(service)

    execution_engine.execute(action.id)

    events = [e.event_type for e in activity.list(entity_type="action", entity_id=action.id)]
    assert events[:2] == ["action.failed", "action.executing"]


# ---------------------------------------------------------------------------
# Missing executor
# ---------------------------------------------------------------------------


def test_a_missing_executor_fails_safely_without_calling_anything(
    db_session: Session, ctx: WorkspaceContext, service: ActionService
) -> None:
    engine_without_executors = ActionExecutionEngine(db_session, ctx, {})
    action = _approved(service)

    with pytest.raises(ActionExecutorUnavailableError):
        engine_without_executors.execute(action.id)

    # The action is untouched: still approved, never transitioned to executing.
    assert service.get(action.id).status is ActionStatus.APPROVED


def test_a_missing_executor_writes_no_additional_activity_event(
    db_session: Session,
    ctx: WorkspaceContext,
    service: ActionService,
    activity: ActivityService,
) -> None:
    engine_without_executors = ActionExecutionEngine(db_session, ctx, {})
    action = _approved(service)
    before = len(activity.list(entity_type="action", entity_id=action.id))

    with pytest.raises(ActionExecutorUnavailableError):
        engine_without_executors.execute(action.id)

    after = len(activity.list(entity_type="action", entity_id=action.id))
    assert after == before


# ---------------------------------------------------------------------------
# Malformed payload
# ---------------------------------------------------------------------------


def test_a_malformed_payload_fails_before_the_executor_is_called(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _approved(service, {"to": "priya@halcyon.example", "subject": "Hi"})

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)

    assert executor.call_count == 0
    assert service.get(action.id).status is ActionStatus.APPROVED


# ---------------------------------------------------------------------------
# Status eligibility: only `approved` may execute
# ---------------------------------------------------------------------------


def test_a_proposed_action_cannot_execute(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _propose_send_email(service)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert executor.call_count == 0


def test_a_rejected_action_cannot_execute(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _propose_send_email(service)
    service.reject(action.id)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert executor.call_count == 0


def test_a_completed_action_cannot_execute_again(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _approved(service)
    execution_engine.execute(action.id)
    assert executor.call_count == 1

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert executor.call_count == 1  # not called again


def test_a_failed_action_cannot_execute_again(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    executor.fail_with = ActionProviderError("boom")
    action = _approved(service)
    execution_engine.execute(action.id)
    assert executor.call_count == 1

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert executor.call_count == 1


def test_an_executing_action_cannot_execute_again(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    action = _approved(service)
    service.start_execution(action.id)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert executor.call_count == 0


# ---------------------------------------------------------------------------
# Idempotency / "concurrent" re-execution
# ---------------------------------------------------------------------------


def test_calling_execute_twice_in_a_row_never_calls_the_executor_twice(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    """Deterministic stand-in for a concurrency test: the engine itself never calls the
    executor more than once per `execute()` call, and a second attempt on an action that
    is no longer `approved` is rejected by `ensure_executable` before it ever reaches
    `ActionService.start_execution`'s row-locked check — the same `approved -> executing`
    guard two truly concurrent requests would race against.

    A genuine two-connection concurrency test (two sessions both calling `start_execution`
    on the same row at once) is not attempted here: it would need a second real database
    connection held open mid-transaction opposite this test's single `db_session`, which
    is exactly the kind of timing-dependent, two-thread test that is brittle under a
    shared test suite. The actual guarantee — `ActionRepository.get_for_update` plus
    `ensure_transition` inside one transaction — already has dedicated, deterministic
    coverage in `test_actions.py::test_an_action_cannot_be_approved_twice` and
    `test_action_execution_contracts.py`'s eligibility tests; the engine adds nothing to
    that guarantee beyond calling `start_execution` itself exactly once per `execute()`.
    """
    action = _approved(service)

    execution_engine.execute(action.id)
    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)

    assert executor.call_count == 1


# ---------------------------------------------------------------------------
# Workspace isolation
# ---------------------------------------------------------------------------


def test_an_action_from_another_workspace_cannot_be_executed(
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    service: ActionService,
    executor: FakeActionExecutor,
) -> None:
    action = _approved(service)

    other_owner = make_user("Bob")
    other_workspace_id = uuid.UUID(make_workspace(other_owner, "Acme"))
    other_ctx = WorkspaceContext(
        workspace_id=other_workspace_id, user_id=uuid.UUID(other_owner.id), role=MemberRole.OWNER
    )
    other_engine = ActionExecutionEngine(db_session, other_ctx, {ActionType.SEND_EMAIL: executor})

    with pytest.raises(NotFoundError):
        other_engine.execute(action.id)
    assert executor.call_count == 0


# ---------------------------------------------------------------------------
# FakeActionExecutor satisfies the protocol (also covered in Block 1's test file;
# repeated here because the engine depends on it directly via the registry).
# ---------------------------------------------------------------------------


def test_fake_action_executor_satisfies_the_executor_protocol(
    executor: FakeActionExecutor,
) -> None:
    from nerva.nerva_actions.executor import ActionExecutor

    assert isinstance(executor, ActionExecutor)


def test_a_generic_execution_error_without_a_specific_subclass_still_fails_safely(
    execution_engine: ActionExecutionEngine, service: ActionService, executor: FakeActionExecutor
) -> None:
    executor.fail_with = ActionExecutionError("unclassified failure, possibly sensitive")
    action = _approved(service)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result == {"error": "The action could not be executed."}
