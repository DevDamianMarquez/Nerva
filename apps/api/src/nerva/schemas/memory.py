"""Schemas for workspace memory — the public contract over
`nerva.services.memory.MemoryService`.

`MemoryCreate.statement`/`attributes` mirror `nerva_memory.content.MemoryContent`
field for field, rather than accepting a loose `content: dict` blob: the content
contract is the same shape for every `MemoryKind` today (see that module's
docstring), so there is no reason to hide it behind an opaque dict at the API
boundary. `MemoryService.remember` still re-validates it against
`MemoryContent` itself — this schema narrows what a client can send, it is not
a substitute for that check.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any, Self

from pydantic import Field, JsonValue, StringConstraints, model_validator

from nerva.core.enums import MemoryKind, MemoryOrigin, MemorySourceType, MemoryStatus
from nerva.schemas.common import RequestModel, ResponseModel


class MemoryCreate(RequestModel):
    kind: MemoryKind = MemoryKind.FACT
    origin: MemoryOrigin
    statement: Annotated[str, StringConstraints(min_length=1, max_length=1000)]
    attributes: dict[str, JsonValue] = Field(default_factory=dict)
    observed_at: datetime
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    contact_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None
    source_conversation_id: uuid.UUID | None = None
    source_analysis_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _confidence_only_when_inferred(self) -> Self:
        """Mirrors `Memory`'s own `confidence_only_when_inferred` CHECK constraint
        (`nerva.models.memory`) at the request boundary: without this, a mismatched
        `origin`/`confidence` pair would reach `MemoryService.remember`, fail only
        at `flush()` with a raw `IntegrityError`, and surface as an unhandled 500
        instead of a clean 422."""
        if (self.origin == MemoryOrigin.INFERRED) != (self.confidence is not None):
            raise ValueError("confidence is required for an inferred memory, and only for one.")
        return self

    @model_validator(mode="after")
    def _at_most_one_subject(self) -> Self:
        """Mirrors `Memory`'s own `at_most_one_subject` CHECK constraint
        (`nerva.models.memory`) at the request boundary, the same reason
        `_confidence_only_when_inferred` exists above: without this, setting more
        than one of `contact_id`/`company_id`/`opportunity_id` would reach
        `MemoryService.remember`, fail only at `flush()` with a raw
        `IntegrityError`, and surface as an unhandled 500 instead of a clean 422."""
        subjects = (self.contact_id, self.company_id, self.opportunity_id)
        if sum(subject is not None for subject in subjects) > 1:
            raise ValueError(
                "A memory concerns at most one subject: contact_id, company_id and "
                "opportunity_id are mutually exclusive."
            )
        return self


class MemorySupersede(RequestModel):
    replacement_id: uuid.UUID


class MemoryDiscard(RequestModel):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class MemoryRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    kind: MemoryKind
    origin: MemoryOrigin
    status: MemoryStatus
    statement: str
    attributes: dict[str, Any]
    schema_version: int
    confidence: float | None
    observed_at: datetime
    contact_id: uuid.UUID | None
    company_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    source_type: MemorySourceType
    source_conversation_id: uuid.UUID | None
    source_analysis_id: uuid.UUID | None
    source_fingerprint: str | None
    resolved_at: datetime | None
    superseded_by_id: uuid.UUID | None
    resolution_reason: str | None
    created_at: datetime
    updated_at: datetime
