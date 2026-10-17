"""Strict, per-type payload contracts for executable actions.

`Action.payload` is free-form JSON at the database and API layer (see
`nerva.models.actions`), so a new `ActionType` needs no migration to be *proposed*. But
no executor may ever act on that JSON directly: `parse_action_payload` is the one place
a raw payload is turned into one of the models below, and every model uses
`extra="forbid"` so an unexpected field is a validation error, not a value an executor
might silently trust.

Only `ActionType` values with a model registered in `EXECUTABLE_PAYLOADS` can reach
execution (see `nerva_actions.eligibility`). `create_task` has none yet — there is no
domain object it would create (no `Task` model exists); adding one is future work, not a
reason to accept an unvalidated freeform payload today.

None of these models carry a provider access token, a `workspace_id`/`user_id` (both
come from `WorkspaceContext`, never from the LLM — see `nerva.core.context`), or a
provider-specific identifier (a Gmail message/thread id). This is why
`UpdateOpportunityStagePayload` does not repeat `opportunity_id`: it is already on the
`Action` row (`Action.opportunity_id`), validated against the caller's workspace by
`ActionService.propose` (`_require_opportunity`) at proposal time — the payload only
adds what that validated reference does not already give the executor. A future
reply-to-an-existing-conversation variant of `send_email` should follow the same
pattern: it would carry a `conversation_id` referencing the workspace's own
`Conversation`, never a raw Gmail thread/message id, and would most likely be a
distinct `ActionType` (e.g. `reply_to_email`) with its own payload model rather than an
optional field bolted onto `SendEmailPayload` — new-email and reply semantics differ
enough (a reply has a fixed recipient and thread already) to deserve separate contracts.
That variant is not implemented in this milestone.
"""

from typing import Annotated, Any, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
)

from nerva.core.enums import ActionType, OpportunityStage
from nerva.nerva_actions.errors import ActionExecutorUnavailableError, ActionNotExecutableError

_Subject = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
_Body = Annotated[str, StringConstraints(min_length=1, max_length=20_000, strip_whitespace=True)]


class SendEmailPayload(BaseModel):
    """The minimum needed to send one new email: who to, subject, body. Deliberately
    excludes cc/bcc, attachments, and any reply-thread linkage — this is new-email
    semantics only (see the module docstring for the reply boundary)."""

    model_config = ConfigDict(extra="forbid")

    recipients: Annotated[list[EmailStr], Field(min_length=1, max_length=10)]
    subject: _Subject
    body: _Body

    @field_validator("recipients")
    @classmethod
    def _lowercase_recipients(cls, value: list[str]) -> list[str]:
        return [address.lower() for address in value]


class UpdateOpportunityStagePayload(BaseModel):
    """Moves the opportunity named by `Action.opportunity_id` to `target_stage`. See the
    module docstring for why the opportunity itself is not repeated inside the payload."""

    model_config = ConfigDict(extra="forbid")

    target_stage: OpportunityStage


type ExecutablePayload = SendEmailPayload | UpdateOpportunityStagePayload

EXECUTABLE_PAYLOADS: dict[ActionType, type[BaseModel]] = {
    ActionType.SEND_EMAIL: SendEmailPayload,
    ActionType.UPDATE_OPPORTUNITY_STAGE: UpdateOpportunityStagePayload,
}


def parse_action_payload(action_type: ActionType, payload: dict[str, Any]) -> ExecutablePayload:
    """Validate `payload` against the strict contract registered for `action_type`.

    Raises `ActionExecutorUnavailableError` if no contract is registered for the type
    (it cannot be executed at all yet), or `ActionNotExecutableError` if the payload does
    not satisfy the registered contract. Never raises a bare `pydantic.ValidationError`:
    callers outside this module should not need to know the payload is Pydantic-backed.
    """
    model = EXECUTABLE_PAYLOADS.get(action_type)
    if model is None:
        raise ActionExecutorUnavailableError(
            f"No executable payload contract is registered for '{action_type.value}'."
        )
    try:
        result = model.model_validate(payload)
    except ValidationError as error:
        raise ActionNotExecutableError(
            f"The payload for '{action_type.value}' does not satisfy its contract."
        ) from error
    # Safe: `EXECUTABLE_PAYLOADS`'s only values are `ExecutablePayload` member classes.
    return cast(ExecutablePayload, result)
