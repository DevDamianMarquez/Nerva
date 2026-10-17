"""Milestone 5 Block 5: `GmailSendExecutor`, the real `ActionExecutor` for
`ActionType.SEND_EMAIL`.

Lives here, not in `nerva_actions/`, for the reason `nerva_actions.executor`'s own
module docstring gives: a real provider adapter lives with its provider client, never
in the domain layer. `tests/test_gmail_send_boundary.py`'s
`test_nerva_actions_package_never_imports_google_or_http_transport_modules` enforces
this mechanically — `nerva_actions/*.py` may never import `nerva.nerva_connect`, a
vendor transport module, or `google*` — so an executor that needs all three cannot
live there. This mirrors `nerva_actions.opportunity_stage_executor.
UpdateOpportunityStageExecutor` on the *domain* side: same `(session, ctx)`
construction, same "no commit of its own for the `Action`" rule, same reliance on
each repository's own `workspace_id` binding (never anything on
`ActionExecutionInput`) for tenant safety.

Resolution order, mirroring `ensure_connected_integration`'s own "before ever calling
`GmailSender` or refreshing a token" ordering:

1. `IntegrationRepository.get_for_provider` — the workspace's own, workspace-scoped
   lookup (never from the action payload: `SendEmailPayload` has no
   workspace/account/integration field to take one from).
2. `ensure_connected_integration` — connected, not disconnected, not needing reauth.
3. `ensure_send_scope_granted` — the stored OAuth grant actually includes
   `gmail.send`, not just `gmail.readonly`.
4. `GoogleCredentialRefresher.refresh_access_token`
   (`nerva.services.integrations`) — the *existing* decrypt/refresh/re-encrypt
   boundary, reused rather than duplicated.
5. `GmailSender.send_email` (the real `GmailAPISender`, or a fake in tests) — the
   one external call.

No commit happens here for the `Action` itself: `ActionExecutionEngine.execute`
calls `ActionService.complete`/`.fail` immediately after `execute()`
returns/raises, exactly as for every other executor.
`GoogleCredentialRefresher.refresh_access_token` may commit the *Integration* row's
status mid-call on a credential failure — a separate row, in its own transaction,
the same thing an ordinary sync already does; see docs/ARCHITECTURE.md §5.5 for why
that does not disturb the `Action`'s own transaction boundary.

Idempotency/crash window: unchanged from Block 4's analysis. `execution_input.
idempotency_key` (the action's own id) is accepted, as every `ActionExecutor` must
be able to accept it, but is not passed to Gmail — `users.messages.send` has no
idempotency-key parameter to pass it to — and nothing here invents one. See
`docs/ARCHITECTURE.md` §5.5.
"""

from pydantic import JsonValue
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionType, IntegrationProvider
from nerva.models import Integration
from nerva.nerva_actions.errors import (
    ActionExecutorUnavailableError,
    ActionNotExecutableError,
    ActionProviderError,
)
from nerva.nerva_actions.executor import ActionExecutionInput, ActionExecutionResult
from nerva.nerva_actions.payloads import SendEmailPayload
from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError
from nerva.nerva_connect.google.oauth import GoogleOAuthClient
from nerva.nerva_connect.google.sender import (
    GmailSender,
    ensure_connected_integration,
    ensure_send_scope_granted,
    translate_send_error,
)
from nerva.repositories.workspace_data import IntegrationRepository
from nerva.services.integrations import (
    GoogleCredentialRefresher,
    IntegrationAuthError,
    IntegrationSyncError,
)

_HEADER_INJECTION_CHARS = ("\r", "\n")


class GmailSendExecutor:
    """One executor, bound to one request's `(session, ctx)` the same way
    `UpdateOpportunityStageExecutor` and `ActionService` already are."""

    action_type = ActionType.SEND_EMAIL

    def __init__(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        oauth_client: GoogleOAuthClient,
        sender: GmailSender,
    ) -> None:
        self._integrations = IntegrationRepository(session, ctx.workspace_id)
        self._credentials = GoogleCredentialRefresher(session, ctx, oauth_client=oauth_client)
        self._sender = sender

    def execute(self, execution_input: ActionExecutionInput) -> ActionExecutionResult:
        payload = execution_input.payload
        if not isinstance(payload, SendEmailPayload):
            # The engine only ever reaches this executor for `ActionType.SEND_EMAIL`,
            # whose payload contract is always `SendEmailPayload` (see
            # `nerva_actions.payloads.EXECUTABLE_PAYLOADS`) — reaching here regardless
            # would mean the executor registry itself is misconfigured, not a bad
            # action (mirrors `UpdateOpportunityStageExecutor`'s identical guard).
            raise ActionExecutorUnavailableError(
                "send_email executor was called with a different action type's payload."
            )
        _reject_header_injection(payload.subject)

        integration = ensure_connected_integration(self._resolve_integration())
        ensure_send_scope_granted(integration)
        access_token = self._obtain_access_token(integration)

        try:
            result = self._sender.send_email(
                access_token,
                recipients=payload.recipients,
                subject=payload.subject,
                body=payload.body,
            )
        except (GoogleAuthError, GoogleApiError) as error:
            raise translate_send_error(error) from error

        # `result.thread_id` is `None` when Gmail's response did not include one
        # (see `GmailSendResult`): omit the key rather than persist a fabricated or
        # null value a future reconciliation step could mistake for real data.
        metadata: dict[str, JsonValue] = (
            {"thread_id": result.thread_id} if result.thread_id is not None else {}
        )
        return ActionExecutionResult(external_reference_id=result.message_id, metadata=metadata)

    def _resolve_integration(self) -> Integration | None:
        return self._integrations.get_for_provider(IntegrationProvider.GOOGLE)

    def _obtain_access_token(self, integration: Integration) -> str:
        try:
            return self._credentials.refresh_access_token(integration)
        except IntegrationAuthError as exc:
            raise ActionExecutorUnavailableError(
                "Gmail rejected the connected account's credentials; it needs to be reconnected."
            ) from exc
        except IntegrationSyncError as exc:
            raise ActionProviderError("Could not reach Gmail to refresh access.") from exc


def _reject_header_injection(subject: str) -> None:
    """`SendEmailPayload.subject` is bounded and whitespace-trimmed (Block 1) but not
    restricted to a single line — a subject containing a raw CR/LF could otherwise
    inject extra headers into the MIME message `GmailAPISender` builds. Checked here,
    before any call to `GmailSender`, rather than inside the sender itself, so
    `GmailSender.send_email`'s documented exception contract (`GoogleAuthError` /
    `GoogleApiError` only) stays accurate."""
    if any(char in subject for char in _HEADER_INJECTION_CHARS):
        raise ActionNotExecutableError("The email subject must not contain line breaks.")
