"""Milestone 5 Block 5: `GmailSendExecutor`, the real `ActionExecutor` for
`send_email`.

Exercises the full, real lifecycle — `ActionService`, `ActionExecutionEngine` and
this executor — against the real (database-backed) test session, with
`tests.fakes.google.FakeGmailSender` standing in for Gmail and
`tests.fakes.google.FakeGoogleOAuthClient` standing in for the OAuth token endpoint.
No test in this file makes a real network call or sends a real email. Mirrors
`test_opportunity_stage_executor.py` in structure; see `test_gmail_send_boundary.py`
for the Block 4/5 contract-level tests this file does not repeat (payload shape,
`ensure_connected_integration`/`ensure_send_scope_granted` unit behavior,
`translate_send_error` classification).
"""

import socket
import uuid
from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.crypto import encrypt_secret
from nerva.core.enums import (
    ActionStatus,
    ActionType,
    IntegrationProvider,
    IntegrationStatus,
    MemberRole,
)
from nerva.core.errors import NotFoundError
from nerva.models import Action, Integration
from nerva.nerva_actions.engine import ActionExecutionEngine
from nerva.nerva_actions.errors import ActionNotExecutableError
from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError
from nerva.nerva_connect.google.send_executor import GmailSendExecutor
from nerva.nerva_connect.google.sender import GmailSendResult
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from nerva.services.activity import ActivityService
from tests.fakes.google import (
    DEFAULT_SCOPE,
    READONLY_ONLY_SCOPE,
    FakeGmailSender,
    FakeGoogleOAuthClient,
)
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

VALID_PAYLOAD = {
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
def actions(db_session: Session, ctx: WorkspaceContext) -> ActionService:
    return ActionService(db_session, ctx)


@pytest.fixture
def activity(db_session: Session, ctx: WorkspaceContext) -> ActivityService:
    return ActivityService(db_session, ctx.workspace_id)


@pytest.fixture
def sender() -> FakeGmailSender:
    return FakeGmailSender()


@pytest.fixture
def executor(
    db_session: Session,
    ctx: WorkspaceContext,
    google_oauth_client: FakeGoogleOAuthClient,
    sender: FakeGmailSender,
) -> GmailSendExecutor:
    return GmailSendExecutor(db_session, ctx, oauth_client=google_oauth_client, sender=sender)


@pytest.fixture
def execution_engine(
    db_session: Session, ctx: WorkspaceContext, executor: GmailSendExecutor
) -> ActionExecutionEngine:
    return ActionExecutionEngine(db_session, ctx, {ActionType.SEND_EMAIL: executor})


def _connect_integration(
    db_session: Session,
    ctx: WorkspaceContext,
    *,
    status: IntegrationStatus = IntegrationStatus.CONNECTED,
    scopes: str = DEFAULT_SCOPE,
    has_refresh_token: bool = True,
) -> Integration:
    """Inserts an `Integration` row directly (bypassing the OAuth HTTP flow, which
    Block 5's executor-level tests do not need to exercise — that is already
    covered by `test_gmail_sync.py`/`test_integrations_oauth.py`)."""
    integration = Integration(
        workspace_id=ctx.workspace_id,
        provider=IntegrationProvider.GOOGLE,
        status=status,
        external_account_id="google-sub-1",
        external_account_email="alex@gmail.example",
        scopes=scopes,
        encrypted_refresh_token=encrypt_secret("fake-refresh-token") if has_refresh_token else None,
    )
    db_session.add(integration)
    db_session.flush()
    return integration


def _propose(actions: ActionService, payload: dict[str, object] | None = None) -> Action:
    return actions.propose(
        ActionCreate.model_validate(
            {
                "type": "send_email",
                "summary": "Reply to Priya",
                "payload": payload if payload is not None else VALID_PAYLOAD,
            }
        )
    )


def _approved(actions: ActionService, payload: dict[str, object] | None = None) -> Action:
    action = _propose(actions, payload)
    return actions.approve(action.id)


# ---------------------------------------------------------------------------
# A. Happy path
# ---------------------------------------------------------------------------


def test_a_successful_send_completes_the_action(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
) -> None:
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.COMPLETED


def test_a_successful_send_passes_exactly_the_payload_fields_to_the_sender(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    execution_engine.execute(action.id)

    assert len(sender.calls) == 1
    _, recipients, subject, body = sender.calls[0]
    assert list(recipients) == ["priya@halcyon.example"]
    assert subject == "Pricing for 3 seats"
    assert body == "Hi Priya, following up on pricing for a 3-seat plan."


def test_a_successful_send_uses_the_refreshed_access_token(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
    google_oauth_client: FakeGoogleOAuthClient,
) -> None:
    google_oauth_client.access_token = "fresh-access-token"
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    execution_engine.execute(action.id)

    access_token_used = sender.calls[0][0]
    assert access_token_used == "fresh-access-token"


def test_gmail_message_id_is_persisted_as_the_external_reference_id(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    sender.next_result = GmailSendResult(message_id="18c9f2b1a2d3e4f5", thread_id="18c9f2b1a2d3")
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.result is not None
    assert result.result["external_reference_id"] == "18c9f2b1a2d3e4f5"


def test_gmail_thread_id_is_persisted_as_safe_metadata(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    sender.next_result = GmailSendResult(message_id="msg_1", thread_id="thread_9")
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.result is not None
    assert result.result["thread_id"] == "thread_9"


def test_a_missing_thread_id_is_omitted_rather_than_fabricated(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    """`GmailSendResult.thread_id` can be `None` (Gmail's response omitted it) — the
    persisted result must not invent one (e.g. by reusing `message_id`); see
    `GmailSendResult`'s docstring and `GmailSendExecutor.execute`."""
    sender.next_result = GmailSendResult(message_id="msg_1", thread_id=None)
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.result is not None
    assert result.result["external_reference_id"] == "msg_1"
    assert "thread_id" not in result.result


# ---------------------------------------------------------------------------
# B. Integration resolution
# ---------------------------------------------------------------------------


def test_a_missing_integration_fails_the_action_without_calling_the_sender(
    execution_engine: ActionExecutionEngine, actions: ActionService, sender: FakeGmailSender
) -> None:
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []


def test_a_disconnected_integration_fails_the_action_without_calling_the_sender(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx, status=IntegrationStatus.DISCONNECTED)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []


def test_an_integration_needing_reauth_fails_the_action_without_calling_the_sender(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx, status=IntegrationStatus.NEEDS_REAUTH)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []


def test_a_readonly_only_integration_requires_reauthorization_before_sending(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    """An account connected before Block 5 (or reconnected under an older scope list)
    only has `gmail.readonly`: it must never be used to try sending — see
    `nerva_connect.google.sender.ensure_send_scope_granted`."""
    _connect_integration(db_session, ctx, scopes=READONLY_ONLY_SCOPE)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []


def test_a_connected_integration_with_a_stale_sync_error_can_still_send(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx, status=IntegrationStatus.ERROR)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.COMPLETED
    assert len(sender.calls) == 1


# ---------------------------------------------------------------------------
# C. Lifecycle / approval requirement
# ---------------------------------------------------------------------------


def test_a_proposed_action_cannot_be_sent(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    action = _propose(actions)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert sender.calls == []


def test_a_rejected_action_cannot_be_sent(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    action = _propose(actions)
    actions.reject(action.id)

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert sender.calls == []


def test_a_completed_action_cannot_be_sent_again(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    action = _approved(actions)
    execution_engine.execute(action.id)
    assert len(sender.calls) == 1

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert len(sender.calls) == 1  # never sent twice


def test_an_invalid_payload_fails_before_any_integration_lookup(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    action = _approved(actions, {"recipients": ["not-an-email"], "subject": "Hi", "body": "x"})

    with pytest.raises(ActionNotExecutableError):
        execution_engine.execute(action.id)
    assert sender.calls == []


def test_a_subject_with_an_embedded_newline_is_rejected_before_any_send(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    """Defends against header injection into the MIME message — see
    `nerva_connect.google.send_executor._reject_header_injection`."""
    _connect_integration(db_session, ctx)
    action = _approved(
        actions,
        {**VALID_PAYLOAD, "subject": "Hi\r\nBcc: attacker@evil.example"},
    )

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []


# ---------------------------------------------------------------------------
# D. Tenant isolation
# ---------------------------------------------------------------------------


def test_an_action_from_another_workspace_cannot_be_executed(
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ctx: WorkspaceContext,
    actions: ActionService,
    executor: GmailSendExecutor,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    other_owner = make_user("Bob")
    other_workspace_id = uuid.UUID(make_workspace(other_owner, "Acme"))
    other_ctx = WorkspaceContext(
        workspace_id=other_workspace_id, user_id=uuid.UUID(other_owner.id), role=MemberRole.OWNER
    )
    other_engine = ActionExecutionEngine(db_session, other_ctx, {ActionType.SEND_EMAIL: executor})

    with pytest.raises(NotFoundError):
        other_engine.execute(action.id)
    assert sender.calls == []


def test_a_workspace_cannot_send_through_another_workspaces_integration(
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    """`GmailSendExecutor` resolves the integration through a `workspace_id`-scoped
    repository — it must find nothing (and fail) when the only connected integration
    belongs to a different workspace, never silently borrow it."""
    other_owner = make_user("Bob")
    other_workspace_id = uuid.UUID(make_workspace(other_owner, "Acme"))
    other_ctx = WorkspaceContext(
        workspace_id=other_workspace_id, user_id=uuid.UUID(other_owner.id), role=MemberRole.OWNER
    )
    _connect_integration(db_session, other_ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []


# ---------------------------------------------------------------------------
# E. Provider failure mapping
# ---------------------------------------------------------------------------


def test_a_sender_authentication_error_fails_the_action_safely(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    sender.fail_with = GoogleAuthError("fake: invalid_grant, token=super-secret-value")
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result is not None
    assert "super-secret-value" not in result.result["error"]


def test_a_429_response_fails_the_action_as_rate_limited(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    sender.fail_with = GoogleApiError("fake: 429 quota exceeded", status_code=429, retry_after=12.0)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result == {"error": "The provider is rate-limiting requests. Retry after 12s."}


def test_a_400_response_fails_the_action_as_a_provider_error(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    sender.fail_with = GoogleApiError("fake: 400 invalid recipient <raw-body>", status_code=400)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result == {"error": "Could not reach the provider."}


def test_a_503_response_fails_the_action_as_a_provider_error(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    sender.fail_with = GoogleApiError("fake: 503 service unavailable", status_code=503)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result == {"error": "Could not reach the provider."}


def test_an_unexpected_transport_failure_fails_the_action_as_a_provider_error(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
) -> None:
    _connect_integration(db_session, ctx)
    sender.fail_with = GoogleApiError("fake: connection reset")
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert result.result == {"error": "Could not reach the provider."}


def test_a_credential_refresh_auth_failure_fails_the_action_safely(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    sender: FakeGmailSender,
    google_oauth_client: FakeGoogleOAuthClient,
) -> None:
    google_oauth_client.fail_refresh = True
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.FAILED
    assert sender.calls == []  # never reached the sender: refreshing the token failed first


def test_a_credential_refresh_auth_failure_marks_the_integration_needing_reauth(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    google_oauth_client: FakeGoogleOAuthClient,
) -> None:
    google_oauth_client.fail_refresh = True
    integration = _connect_integration(db_session, ctx)
    action = _approved(actions)

    execution_engine.execute(action.id)

    db_session.refresh(integration)
    assert integration.status is IntegrationStatus.NEEDS_REAUTH


# ---------------------------------------------------------------------------
# F. Safety: no secrets, no network
# ---------------------------------------------------------------------------


def test_no_result_ever_contains_the_email_body(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
) -> None:
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.result is not None
    assert "Hi Priya" not in str(result.result)


def test_a_successful_send_writes_exactly_the_action_lifecycle_events(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    activity: ActivityService,
) -> None:
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    execution_engine.execute(action.id)

    events = [e.event_type for e in activity.list(entity_type="action", entity_id=action.id)]
    assert events == ["action.completed", "action.executing", "action.approved", "action.proposed"]


def test_the_executor_makes_no_network_calls(
    db_session: Session,
    ctx: WorkspaceContext,
    execution_engine: ActionExecutionEngine,
    actions: ActionService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("the executor must never open a socket in the unit test suite")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    _connect_integration(db_session, ctx)
    action = _approved(actions)

    result = execution_engine.execute(action.id)

    assert result.status is ActionStatus.COMPLETED
