"""Milestone 5 Block 1: typed executable-action payloads, the provider-neutral
`ActionExecutor` protocol, and the eligibility gate a future execution engine will call
before ever touching an executor.

No execution engine exists yet (that is later Milestone 5 work) — these tests exercise
the contracts directly. Nothing here makes a network call or depends on a real provider.
"""

import dataclasses
import uuid
from collections.abc import Callable

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionStatus, ActionType, MemberRole, OpportunityStage
from nerva.models import Action
from nerva.nerva_actions.eligibility import build_execution_input, ensure_executable
from nerva.nerva_actions.errors import (
    ActionExecutionError,
    ActionExecutorUnavailableError,
    ActionNotExecutableError,
)
from nerva.nerva_actions.executor import ActionExecutionInput, ActionExecutionResult, ActionExecutor
from nerva.nerva_actions.lifecycle import can_transition
from nerva.nerva_actions.payloads import (
    SendEmailPayload,
    UpdateOpportunityStagePayload,
    parse_action_payload,
)
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from tests.fakes.actions import FakeActionExecutor
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

VALID_SEND_EMAIL_PAYLOAD = {
    "recipients": ["priya@halcyon.example"],
    "subject": "Pricing for 3 seats",
    "body": "Hi Priya, following up on pricing for a 3-seat plan.",
}

# ---------------------------------------------------------------------------
# 1. Strict payload validation: SendEmailPayload
# ---------------------------------------------------------------------------


def test_send_email_payload_accepts_a_minimal_valid_payload() -> None:
    payload = SendEmailPayload.model_validate(VALID_SEND_EMAIL_PAYLOAD)

    assert payload.recipients == ["priya@halcyon.example"]
    assert payload.subject == "Pricing for 3 seats"


def test_send_email_payload_lowercases_recipients() -> None:
    payload = SendEmailPayload.model_validate(
        {**VALID_SEND_EMAIL_PAYLOAD, "recipients": ["Priya@Halcyon.Example"]}
    )

    assert payload.recipients == ["priya@halcyon.example"]


@pytest.mark.parametrize(
    "extra_field",
    ["workspace_id", "user_id", "access_token", "provider_message_id", "cc"],
)
def test_send_email_payload_rejects_unknown_fields(extra_field: str) -> None:
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate({**VALID_SEND_EMAIL_PAYLOAD, extra_field: "x"})


def test_send_email_payload_rejects_an_invalid_recipient_address() -> None:
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate(
            {**VALID_SEND_EMAIL_PAYLOAD, "recipients": ["not-an-email"]}
        )


def test_send_email_payload_rejects_no_recipients() -> None:
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate({**VALID_SEND_EMAIL_PAYLOAD, "recipients": []})


def test_send_email_payload_rejects_too_many_recipients() -> None:
    too_many = [f"person{i}@halcyon.example" for i in range(11)]
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate({**VALID_SEND_EMAIL_PAYLOAD, "recipients": too_many})


@pytest.mark.parametrize("field", ["subject", "body"])
def test_send_email_payload_rejects_an_empty_field(field: str) -> None:
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate({**VALID_SEND_EMAIL_PAYLOAD, field: ""})


def test_send_email_payload_rejects_an_oversized_body() -> None:
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate({**VALID_SEND_EMAIL_PAYLOAD, "body": "x" * 20_001})


# ---------------------------------------------------------------------------
# 2. Strict payload validation: UpdateOpportunityStagePayload
# ---------------------------------------------------------------------------


def test_update_opportunity_stage_payload_accepts_a_known_stage() -> None:
    payload = UpdateOpportunityStagePayload.model_validate({"target_stage": "qualified"})

    assert payload.target_stage is OpportunityStage.QUALIFIED


def test_update_opportunity_stage_payload_rejects_an_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        UpdateOpportunityStagePayload.model_validate({"target_stage": "not-a-stage"})


@pytest.mark.parametrize("extra_field", ["opportunity_id", "workspace_id", "user_id"])
def test_update_opportunity_stage_payload_rejects_unknown_fields(extra_field: str) -> None:
    with pytest.raises(ValidationError):
        UpdateOpportunityStagePayload.model_validate({"target_stage": "won", extra_field: "x"})


# ---------------------------------------------------------------------------
# 3. The action_type -> payload contract registry
# ---------------------------------------------------------------------------


def test_parse_action_payload_returns_the_registered_model() -> None:
    result = parse_action_payload(ActionType.SEND_EMAIL, VALID_SEND_EMAIL_PAYLOAD)

    assert isinstance(result, SendEmailPayload)


def test_parse_action_payload_raises_for_a_type_with_no_registered_contract() -> None:
    with pytest.raises(ActionExecutorUnavailableError):
        parse_action_payload(ActionType.CREATE_TASK, {"title": "Call Tomás"})


def test_parse_action_payload_raises_for_an_invalid_payload() -> None:
    with pytest.raises(ActionNotExecutableError):
        parse_action_payload(ActionType.SEND_EMAIL, {"to": "priya@halcyon.example"})


# ---------------------------------------------------------------------------
# 4. Execution eligibility, against real (database-backed) actions
# ---------------------------------------------------------------------------


@pytest.fixture
def setup(make_user: MakeUser, make_workspace: MakeWorkspace) -> tuple[Account, uuid.UUID]:
    owner = make_user("Alice")
    return owner, uuid.UUID(make_workspace(owner, "Northwind"))


@pytest.fixture
def service(db_session: Session, setup: tuple[Account, uuid.UUID]) -> ActionService:
    owner, workspace_id = setup
    ctx = WorkspaceContext(
        workspace_id=workspace_id, user_id=uuid.UUID(owner.id), role=MemberRole.OWNER
    )
    return ActionService(db_session, ctx)


def _propose_send_email(service: ActionService, payload: dict[str, object]) -> Action:
    return service.propose(
        ActionCreate.model_validate(
            {"type": "send_email", "summary": "Reply to Priya", "payload": payload}
        )
    )


def test_a_proposed_action_is_not_executable(service: ActionService) -> None:
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)

    with pytest.raises(ActionNotExecutableError) as error:
        ensure_executable(action)
    assert error.value.status is ActionStatus.PROPOSED


def test_a_rejected_action_is_not_executable(service: ActionService) -> None:
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)
    service.reject(action.id, reason="Not now")

    with pytest.raises(ActionNotExecutableError) as error:
        ensure_executable(service.get(action.id))
    assert error.value.status is ActionStatus.REJECTED


def test_an_approved_action_is_executable(service: ActionService) -> None:
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)
    service.approve(action.id)

    payload = ensure_executable(service.get(action.id))

    assert isinstance(payload, SendEmailPayload)
    assert payload.recipients == ["priya@halcyon.example"]


def test_an_executing_action_cannot_be_executed_again(service: ActionService) -> None:
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)
    service.approve(action.id)
    service.start_execution(action.id)

    with pytest.raises(ActionNotExecutableError) as error:
        ensure_executable(service.get(action.id))
    assert error.value.status is ActionStatus.EXECUTING


@pytest.mark.parametrize("terminal", ["complete", "fail"])
def test_a_finished_action_cannot_be_executed_again(service: ActionService, terminal: str) -> None:
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)
    service.approve(action.id)
    service.start_execution(action.id)
    if terminal == "complete":
        service.complete(action.id)
    else:
        service.fail(action.id, error="Mailbox quota exceeded")

    with pytest.raises(ActionNotExecutableError):
        ensure_executable(service.get(action.id))


def test_an_approved_action_with_a_malformed_payload_is_not_executable(
    service: ActionService,
) -> None:
    """Nothing at `propose` time enforces `SendEmailPayload` (see
    `docs/ARCHITECTURE.md` §5): a payload can be approved by a human without ever having
    been validated against its type's contract. `ensure_executable` is what actually
    stops it from reaching an executor."""
    action = _propose_send_email(service, {"to": "priya@halcyon.example", "subject": "Hi"})
    service.approve(action.id)

    with pytest.raises(ActionNotExecutableError):
        ensure_executable(service.get(action.id))


def test_eligibility_agrees_with_the_lifecycle_transition_table(service: ActionService) -> None:
    """`ensure_executable`'s status check must never diverge from
    `nerva_actions.lifecycle`'s transition table — it is not a second state machine."""
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)
    service.approve(action.id)
    approved = service.get(action.id)

    assert can_transition(approved.status, ActionStatus.EXECUTING)
    ensure_executable(approved)  # does not raise


def test_build_execution_input_uses_the_action_id_as_the_idempotency_key(
    service: ActionService,
) -> None:
    action = _propose_send_email(service, VALID_SEND_EMAIL_PAYLOAD)
    service.approve(action.id)

    execution_input = build_execution_input(service.get(action.id))

    assert execution_input.idempotency_key == str(action.id)
    assert execution_input.action_type is ActionType.SEND_EMAIL
    assert isinstance(execution_input.payload, SendEmailPayload)


def test_execution_input_carries_no_workspace_or_user_identity() -> None:
    """The contract itself, not just its contents, excludes identity: no field could
    ever carry a workspace or user id, LLM-supplied or otherwise."""
    field_names = {f.name for f in dataclasses.fields(ActionExecutionInput)}

    assert field_names == {"action_type", "payload", "idempotency_key"}
    assert "workspace_id" not in field_names
    assert "user_id" not in field_names


# ---------------------------------------------------------------------------
# 5. The ActionExecutor protocol and its fake
# ---------------------------------------------------------------------------


def test_fake_action_executor_implements_the_protocol() -> None:
    fake = FakeActionExecutor(ActionType.SEND_EMAIL)

    assert isinstance(fake, ActionExecutor)


def test_fake_action_executor_records_calls_and_returns_the_scripted_result() -> None:
    scripted = ActionExecutionResult(external_reference_id="msg_123", metadata={"provider": "fake"})
    fake = FakeActionExecutor(ActionType.SEND_EMAIL, result=scripted)
    execution_input = ActionExecutionInput(
        action_type=ActionType.SEND_EMAIL,
        payload=SendEmailPayload.model_validate(VALID_SEND_EMAIL_PAYLOAD),
        idempotency_key=str(uuid.uuid4()),
    )

    result = fake.execute(execution_input)

    assert result is scripted
    assert fake.calls == [execution_input]
    assert fake.call_count == 1


def test_fake_action_executor_can_be_scripted_to_fail() -> None:
    fake = FakeActionExecutor(ActionType.SEND_EMAIL)
    fake.fail_with = ActionExecutionError("fake: scripted provider failure")
    execution_input = ActionExecutionInput(
        action_type=ActionType.SEND_EMAIL,
        payload=SendEmailPayload.model_validate(VALID_SEND_EMAIL_PAYLOAD),
        idempotency_key=str(uuid.uuid4()),
    )

    with pytest.raises(ActionExecutionError, match="scripted provider failure"):
        fake.execute(execution_input)
