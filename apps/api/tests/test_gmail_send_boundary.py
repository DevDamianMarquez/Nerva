"""Milestone 5 Blocks 4 & 5: the `send_email` contract and the Gmail provider
boundary the real `send_email` `ActionExecutor` depends on.

Nothing in *this file* makes a network call or touches a real Google account — see
`nerva.nerva_connect.google.sender` and `docs/ARCHITECTURE.md` §5.4/§5.5.
`test_action_execution_contracts.py` (Block 1) already exhaustively covers
`SendEmailPayload`'s own strict validation (recipients, subject/body bounds,
`extra="forbid"`, invalid addresses); this file does not repeat that, only reaffirms
the specific decisions Block 4 reviewed (no cc/bcc, no thread/conversation id) and
the boundary-level contracts both blocks added. See `test_gmail_send_executor.py`
for the real executor's own end-to-end lifecycle tests.
"""

import ast
import inspect
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

import nerva.nerva_actions as nerva_actions_package
from nerva.core.enums import ActionType, IntegrationProvider, IntegrationStatus
from nerva.models import Integration
from nerva.nerva_actions.errors import (
    ActionExecutorUnavailableError,
    ActionProviderError,
    ActionRateLimitError,
)
from nerva.nerva_actions.executor import ActionExecutionInput, ActionExecutionResult
from nerva.nerva_actions.payloads import SendEmailPayload
from nerva.nerva_connect.google.errors import (
    GoogleApiError,
    GoogleApiErrorCategory,
    GoogleAuthError,
)
from nerva.nerva_connect.google.oauth import SCOPE_GMAIL_SEND
from nerva.nerva_connect.google.sender import (
    GmailSender,
    GmailSendResult,
    ensure_connected_integration,
    ensure_send_scope_granted,
    translate_send_error,
)
from tests.fakes.google import DEFAULT_SCOPE, READONLY_ONLY_SCOPE, FakeGmailSender

VALID_SEND_EMAIL_PAYLOAD = {
    "recipients": ["priya@halcyon.example"],
    "subject": "Pricing for 3 seats",
    "body": "Hi Priya, following up on pricing for a 3-seat plan.",
}


def _integration(*, status: IntegrationStatus, scopes: str = DEFAULT_SCOPE) -> Integration:
    return Integration(
        workspace_id=uuid.uuid4(),
        id=uuid.uuid4(),
        provider=IntegrationProvider.GOOGLE,
        status=status,
        external_account_id="google-sub-1",
        external_account_email="alex@gmail.example",
        scopes=scopes,
    )


# ---------------------------------------------------------------------------
# 2. The send_email contract: reaffirming this block's review found no gap
# ---------------------------------------------------------------------------


def test_send_email_payload_fields_are_unchanged_by_this_block() -> None:
    """This block's review concluded the existing contract (recipients, subject, body)
    already correctly represents `send_email`'s business intent, and deliberately adds
    no cc/bcc (not justified by any existing use case) and no conversation/thread/
    message id (that belongs to the future, distinct `reply_to_email` type) — see
    `nerva_actions.payloads`'s own module docstring and `docs/ARCHITECTURE.md` §5.4."""
    assert set(SendEmailPayload.model_fields) == {"recipients", "subject", "body"}


@pytest.mark.parametrize("extra_field", ["bcc", "thread_id", "conversation_id", "message_id"])
def test_send_email_payload_still_rejects_fields_this_block_considered_and_declined(
    extra_field: str,
) -> None:
    with pytest.raises(ValidationError):
        SendEmailPayload.model_validate({**VALID_SEND_EMAIL_PAYLOAD, extra_field: "x"})


# ---------------------------------------------------------------------------
# F. The provider boundary can be replaced with a fake
# ---------------------------------------------------------------------------


def test_fake_gmail_sender_satisfies_the_protocol() -> None:
    assert isinstance(FakeGmailSender(), GmailSender)


def test_fake_gmail_sender_records_calls_and_returns_the_scripted_result() -> None:
    scripted = GmailSendResult(message_id="msg_123", thread_id="thread_9")
    fake = FakeGmailSender(result=scripted)

    result = fake.send_email(
        "fake-access-token",
        recipients=["priya@halcyon.example"],
        subject="Pricing for 3 seats",
        body="Hi Priya",
    )

    assert result is scripted
    assert fake.calls == [
        ("fake-access-token", ["priya@halcyon.example"], "Pricing for 3 seats", "Hi Priya")
    ]


def test_fake_gmail_sender_can_be_scripted_to_fail() -> None:
    fake = FakeGmailSender()
    fake.fail_with = GoogleApiError("fake: Gmail returned 503")

    with pytest.raises(GoogleApiError):
        fake.send_email("token", recipients=["a@b.example"], subject="s", body="b")


# ---------------------------------------------------------------------------
# G. Domain/action code does not import the Gmail SDK or any Google module
# ---------------------------------------------------------------------------


FORBIDDEN_IMPORT_PREFIXES = ("googleapiclient", "google", "httpx", "nerva.nerva_connect")


def _imported_module_names(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_nerva_actions_package_never_imports_google_or_http_transport_modules() -> None:
    package_dir = Path(nerva_actions_package.__file__).parent
    for source_path in package_dir.glob("*.py"):
        imported = _imported_module_names(source_path)
        for forbidden in FORBIDDEN_IMPORT_PREFIXES:
            offenders = {
                name for name in imported if name == forbidden or name.startswith(forbidden + ".")
            }
            assert not offenders, f"{source_path.name} imports {offenders}"


# ---------------------------------------------------------------------------
# H. Provider errors map into the neutral Action execution error hierarchy
# ---------------------------------------------------------------------------


def test_a_google_auth_error_translates_to_executor_unavailable() -> None:
    error = translate_send_error(GoogleAuthError("fake: invalid_grant"))

    assert isinstance(error, ActionExecutorUnavailableError)
    assert "invalid_grant" not in str(error)  # the raw Google error text is not leaked


def test_a_google_api_error_translates_to_a_provider_error() -> None:
    error = translate_send_error(GoogleApiError("fake: Gmail returned 503 with <secret/>"))

    assert isinstance(error, ActionProviderError)
    assert "<secret/>" not in str(error)


# ---------------------------------------------------------------------------
# I. A missing Google integration is represented safely
# ---------------------------------------------------------------------------


def test_a_missing_integration_is_rejected_before_any_network_call() -> None:
    with pytest.raises(ActionExecutorUnavailableError):
        ensure_connected_integration(None)


# ---------------------------------------------------------------------------
# J. Reauthentication / disconnected states are represented safely
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [IntegrationStatus.NEEDS_REAUTH, IntegrationStatus.DISCONNECTED])
def test_an_unusable_integration_is_rejected_before_any_network_call(
    status: IntegrationStatus,
) -> None:
    with pytest.raises(ActionExecutorUnavailableError):
        ensure_connected_integration(_integration(status=status))


def test_a_connected_integration_with_a_stale_sync_error_is_still_usable() -> None:
    """A failed *import* sync has no bearing on whether sending would work — see
    `ensure_connected_integration`'s docstring."""
    integration = _integration(status=IntegrationStatus.ERROR)

    assert ensure_connected_integration(integration) is integration


def test_a_connected_integration_is_usable() -> None:
    integration = _integration(status=IntegrationStatus.CONNECTED)

    assert ensure_connected_integration(integration) is integration


# ---------------------------------------------------------------------------
# K. Idempotency / crash-window: documented in docs/ARCHITECTURE.md §5.4;
# tested here at the contract level this block can honestly support.
# ---------------------------------------------------------------------------


def test_the_send_email_idempotency_key_is_still_the_action_id() -> None:
    """Unchanged from Block 1/2: no Gmail-specific idempotency key is introduced,
    because Gmail's `users.messages.send` has none to pass through — see
    `translate_send_error`'s docstring and §5.4."""
    execution_input = ActionExecutionInput(
        action_type=ActionType.SEND_EMAIL,
        payload=SendEmailPayload.model_validate(VALID_SEND_EMAIL_PAYLOAD),
        idempotency_key=str(uuid.uuid4()),
    )

    assert execution_input.idempotency_key  # present, but see §5.4: Gmail cannot use it


def test_gmail_send_result_carries_what_a_future_executor_would_need_to_reconcile() -> None:
    """`message_id` is exactly what `ActionExecutionResult.external_reference_id`
    would store on success (see `GmailSendResult`'s docstring) — the only piece of
    information a future reconciliation step could possibly use, and only for an
    action that *reached* `completed`. It is still not a crash-window guarantee: see
    §5.4 for why a crash between Gmail accepting the message and that commit leaves
    nothing to reconcile against."""
    result = GmailSendResult(message_id="18c9f2b1a2d3e4f5", thread_id="18c9f2b1a2d3e4f5")

    execution_result = ActionExecutionResult(
        external_reference_id=result.message_id, metadata={"thread_id": result.thread_id}
    )

    assert execution_result.external_reference_id == "18c9f2b1a2d3e4f5"


# ---------------------------------------------------------------------------
# L. Workspace context is never taken from the payload or this boundary
# ---------------------------------------------------------------------------


def test_send_email_payload_has_no_workspace_or_account_identity() -> None:
    assert "workspace_id" not in SendEmailPayload.model_fields
    assert "user_id" not in SendEmailPayload.model_fields
    assert "account_id" not in SendEmailPayload.model_fields


def test_gmail_sender_protocol_never_accepts_a_workspace_or_account_identifier() -> None:
    params = set(inspect.signature(GmailSender.send_email).parameters)

    assert params == {"self", "access_token", "recipients", "subject", "body"}


def test_ensure_connected_integration_takes_no_workspace_or_user_identifier() -> None:
    params = set(inspect.signature(ensure_connected_integration).parameters)

    assert params == {"integration"}


# ---------------------------------------------------------------------------
# M. Milestone 5 Block 5: OAuth scope gate for send
# ---------------------------------------------------------------------------


def test_a_connected_integration_with_the_send_scope_is_usable_for_sending() -> None:
    integration = _integration(status=IntegrationStatus.CONNECTED, scopes=DEFAULT_SCOPE)

    ensure_send_scope_granted(integration)  # does not raise


def test_a_readonly_only_integration_is_rejected_for_sending() -> None:
    """An account connected before Block 5 (or under an older scope list) only has
    `gmail.readonly` — it must never be used to try sending; see
    `ensure_send_scope_granted`'s docstring."""
    integration = _integration(status=IntegrationStatus.CONNECTED, scopes=READONLY_ONLY_SCOPE)

    with pytest.raises(ActionExecutorUnavailableError):
        ensure_send_scope_granted(integration)


def test_an_integration_with_no_scopes_recorded_is_rejected_for_sending() -> None:
    integration = _integration(status=IntegrationStatus.CONNECTED, scopes="")

    with pytest.raises(ActionExecutorUnavailableError):
        ensure_send_scope_granted(integration)


def test_send_scope_constant_is_requested_in_the_oauth_scope_list() -> None:
    from nerva.nerva_connect.google.oauth import SCOPES

    assert SCOPE_GMAIL_SEND in SCOPES.split()


# ---------------------------------------------------------------------------
# N. Milestone 5 Block 5: GoogleApiError preserves safe HTTP status metadata
# ---------------------------------------------------------------------------


def test_google_api_error_without_a_status_is_categorized_unknown() -> None:
    error = GoogleApiError("transport failure")

    assert error.category is GoogleApiErrorCategory.UNKNOWN
    assert error.status_code is None


@pytest.mark.parametrize(
    ("status_code", "expected_category"),
    [
        (429, GoogleApiErrorCategory.RATE_LIMIT),
        (400, GoogleApiErrorCategory.CLIENT_ERROR),
        (404, GoogleApiErrorCategory.CLIENT_ERROR),
        (500, GoogleApiErrorCategory.SERVER_ERROR),
        (503, GoogleApiErrorCategory.SERVER_ERROR),
    ],
)
def test_google_api_error_categorizes_known_status_codes(
    status_code: int, expected_category: GoogleApiErrorCategory
) -> None:
    error = GoogleApiError("fake failure", status_code=status_code)

    assert error.category is expected_category


def test_google_api_error_never_requires_a_retry_after() -> None:
    error = GoogleApiError("fake 429", status_code=429)

    assert error.retry_after is None


# ---------------------------------------------------------------------------
# O. Milestone 5 Block 5: translate_send_error distinguishes provider failures
# ---------------------------------------------------------------------------


def test_a_429_error_translates_to_a_rate_limit_error_with_retry_after() -> None:
    error = translate_send_error(
        GoogleApiError("fake: quota exceeded <raw/>", status_code=429, retry_after=30.0)
    )

    assert isinstance(error, ActionRateLimitError)
    assert error.retry_after == 30.0
    assert "<raw/>" not in str(error)


def test_a_429_error_without_a_retry_after_header_still_translates_safely() -> None:
    error = translate_send_error(GoogleApiError("fake: quota exceeded", status_code=429))

    assert isinstance(error, ActionRateLimitError)
    assert error.retry_after is None


@pytest.mark.parametrize("status_code", [400, 403, 404])
def test_a_4xx_error_translates_to_a_provider_error(status_code: int) -> None:
    error = translate_send_error(
        GoogleApiError(f"fake: {status_code} <secret-body/>", status_code=status_code)
    )

    assert isinstance(error, ActionProviderError)
    assert not isinstance(error, ActionRateLimitError)
    assert "<secret-body/>" not in str(error)


@pytest.mark.parametrize("status_code", [500, 502, 503])
def test_a_5xx_error_translates_to_a_provider_error(status_code: int) -> None:
    error = translate_send_error(GoogleApiError("fake: outage", status_code=status_code))

    assert isinstance(error, ActionProviderError)
    assert not isinstance(error, ActionRateLimitError)


def test_an_unclassifiable_provider_failure_still_translates_to_a_safe_provider_error() -> None:
    """No HTTP status at all (e.g. a transport-level failure) — Block 4's original
    catch-all behavior, unchanged."""
    error = translate_send_error(GoogleApiError("fake: connection reset"))

    assert isinstance(error, ActionProviderError)
    assert not isinstance(error, ActionRateLimitError)
