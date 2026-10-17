"""Structured, validated content for a `Memory` row.

`Memory.content` is a validated JSONB payload — never a raw email body, a raw
provider response, or unstructured prose a model invented without shape (see
`nerva.models.memory`'s module docstring). `MEMORY_SCHEMA_VERSION` is stamped
onto every row at write time (`Memory.schema_version`) so this shape can change
later without a migration touching rows already written, the same way
`nerva_ai.schemas.SCHEMA_VERSION` feeds `ConversationAnalysis.schema_version`.

One shape for every `MemoryKind` in v0.1: `statement` is the one thing every
memory must have — a bounded, non-empty sentence, so a memory can never be an
empty or unbounded blob — and `attributes` is where kind-specific structured
detail goes once a concrete need for one exists (e.g. a preferred contact
channel, a mentioned renewal date). A per-kind contract, mirroring
`nerva_actions.payloads.EXECUTABLE_PAYLOADS`, is a straightforward later change
should kinds genuinely grow different shapes — not needed yet.
"""

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, ValidationError

from nerva.nerva_memory.errors import MemoryContentError

MEMORY_SCHEMA_VERSION = 1

_Statement = Annotated[str, StringConstraints(min_length=1, max_length=1000, strip_whitespace=True)]


class MemoryContent(BaseModel):
    """`statement` is what Nerva actually recalls, and may one day show a human
    or feed back into a prompt. `attributes` carries optional, already-structured
    detail — validated JSON, never a raw email body or provider response."""

    model_config = ConfigDict(extra="forbid")

    statement: _Statement
    attributes: dict[str, JsonValue] = Field(default_factory=dict)


def parse_memory_content(content: dict[str, Any]) -> MemoryContent:
    """Validate `content` against `MemoryContent`. Never raises a bare
    `pydantic.ValidationError`: callers outside this module should not need to
    know this contract is Pydantic-backed."""
    try:
        return MemoryContent.model_validate(content)
    except ValidationError as error:
        raise MemoryContentError("Memory content does not satisfy its contract.") from error
