"""Structured-output schema validation for `GroqAgentReasoningProvider`'s
`response_format` (M7.1g, wire shape replaced by M7.1i.1) — no network call,
no Groq credentials.

Unlike `test_openai_agent_reasoning_schema.py` (which asks the `openai` SDK's
own `type_to_response_format_param` helper what it would send), this module
captures the *actual* `response_format` dict `GroqAgentReasoningProvider.
reason` passes to a mocked `client.chat.completions.create` — the `groq` SDK
exposes no equivalent schema-introspection helper, so the only honest way to
test "what Groq is actually asked for" is to intercept the real call. This
also exercises `nerva_ai.providers.groq_agent_reasoning._strict_schema`
directly against the Groq-specific flat wire contract
(`nerva_ai.providers._groq_send_email_wire`).

M7.1i.1 replaced the shared, nested wire contract this module used to
validate (`{"decision": {...}}`) with a Groq-only flat one
(`{"decision": "no_action", "reason": ..., "confidence": ...}`) — see
`groq_agent_reasoning`'s own module docstring for why. This module's own
job is proving the *shape actually sent to Groq* reflects that: no nested
envelope, `decision` a plain top-level string, root schema a plain object
(never a root-level `anyOf`), and the same closed-action-type /
bounded-evidence/recipient guarantees the old shared contract had.

This module tests the *shape* of what Groq is asked to return — it proves
nothing about `nerva_core.proposal_policy`, which remains the unconditional
authority on whether a validated decision is actually safe to act on (see
`groq_agent_reasoning`'s own module docstring)."""

from types import SimpleNamespace
from typing import Any

from nerva.core.config import Settings
from nerva.core.enums import ActionType, ConversationCategory, ConversationPriority
from nerva.nerva_ai.agent_client import AgentReasoningInput, ReasoningAnalysisContext
from nerva.nerva_ai.providers.groq_agent_reasoning import GroqAgentReasoningProvider

SOME_ANALYSIS = ReasoningAnalysisContext(
    category=ConversationCategory.SALES_LEAD,
    priority=ConversationPriority.MEDIUM,
    sentiment=None,
    requires_response=True,
    commercial_signal=True,
    confidence=0.8,
    summary="Prospect asked about pricing for 3 seats.",
    intent="request_quote",
    suggested_next_step="Send pricing information.",
    estimated_value=None,
    deadline=None,
)

SOME_REQUEST = AgentReasoningInput(
    subject="Pricing for 3 seats",
    contact_display_name="Priya Nair",
    messages=(),
    analysis=SOME_ANALYSIS,
    capabilities=(ActionType.SEND_EMAIL,),
)


class _CapturingCompletions:
    """Records the exact kwargs passed to `.create()` and returns a minimal
    valid, FLAT NO_ACTION completion — this module only cares about the
    *request* sent, never the response mapping (that is
    `test_groq_agent_reasoning_provider.py`'s job)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        content = (
            '{"decision": "no_action", '
            '"reason": "Nothing warrants a reply yet.", "confidence": 0.9}'
        )
        message = SimpleNamespace(content=content)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
            model="openai/gpt-oss-120b",
        )


def _captured_schema() -> dict[str, Any]:
    completions = _CapturingCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    provider = GroqAgentReasoningProvider(
        Settings(
            groq_api_key="gsk-test",
            nerva_agent_reasoning_provider="groq",
            nerva_groq_agent_reasoning_model="openai/gpt-oss-120b",
        ),
        client=client,  # type: ignore[arg-type]
    )
    provider.reason(SOME_REQUEST)

    assert len(completions.calls) == 1
    call = completions.calls[0]
    response_format = call["response_format"]
    assert isinstance(response_format, dict)
    return response_format


def _all_object_schemas(node: object) -> list[dict[str, Any]]:
    """Every dict in the schema tree whose own `"type"` is `"object"` —
    walks `$defs` and nested `properties`/`items`/`anyOf` alike, mirroring
    `test_openai_agent_reasoning_schema.py`'s own walker exactly."""
    found: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("type") == "object":
            found.append(node)
        for value in node.values():
            found.extend(_all_object_schemas(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_all_object_schemas(item))
    return found


def _collect_values(node: object, *, keys: tuple[str, ...]) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                if isinstance(value, str):
                    found.add(value)
                elif isinstance(value, list):
                    found.update(item for item in value if isinstance(item, str))
            else:
                found |= _collect_values(value, keys=keys)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_values(item, keys=keys)
    return found


# ---------------------------------------------------------------------------
# A. response_format envelope
# ---------------------------------------------------------------------------


def test_response_format_uses_json_schema_mode_not_json_object_mode() -> None:
    """Groq's own documentation (verified in the M7.1g.1 audit) draws a hard
    line between JSON Object mode (no schema enforcement) and Structured
    Outputs/JSON Schema mode — this provider must only ever request the
    latter."""
    response_format = _captured_schema()

    assert response_format["type"] == "json_schema"


def test_json_schema_envelope_has_a_name_and_is_strict() -> None:
    response_format = _captured_schema()
    json_schema = response_format["json_schema"]

    assert json_schema["name"] == "agent_reasoning_decision"
    assert json_schema["strict"] is True
    assert isinstance(json_schema["schema"], dict)


# ---------------------------------------------------------------------------
# B. strict-mode structural requirements, recursively
# ---------------------------------------------------------------------------


def test_every_object_schema_sets_additional_properties_false() -> None:
    """Structured Outputs' `strict: true` requires exactly this on every
    object in the schema — verified for every object node, `$defs`
    included, not just the ones named by hand."""
    schema = _captured_schema()["json_schema"]["schema"]
    objects = _all_object_schemas(schema)

    assert objects, "expected at least one object schema"
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj


def test_required_equals_properties_for_every_object_including_nullable_fields() -> None:
    """The M7.1i.1 equivalent of the M7.1g.1 risk: `summary`/`payload`/
    `evidence` are nullable (`T | None`) on the flat wire model, so plain
    `model_json_schema()` would omit them from `required` — `strict` mode
    demands every declared property be listed there regardless of
    nullability. `_strict_schema` must have corrected this for every object
    node, root included."""
    schema = _captured_schema()["json_schema"]["schema"]
    for obj in _all_object_schemas(schema):
        properties = set(obj.get("properties", {}))
        if not properties:
            continue
        assert set(obj.get("required", [])) == properties, obj


def test_no_free_form_additional_properties_value_exists_anywhere() -> None:
    schema = _captured_schema()["json_schema"]["schema"]
    objects = _all_object_schemas(schema)

    for obj in objects:
        additional = obj.get("additionalProperties")
        assert additional is False or additional is None, (
            f"found a non-strict additionalProperties value: {additional!r}"
        )


# ---------------------------------------------------------------------------
# C. flat shape: no nested envelope, root is a plain object, no root anyOf
# ---------------------------------------------------------------------------


def test_root_schema_is_a_plain_object_not_a_root_level_anyof() -> None:
    """Groq's own documentation (consulted during the M7.1i audit) requires
    the root schema to be a plain object — `anyOf` is only documented as
    supported nested inside a property, never at the root."""
    schema = _captured_schema()["json_schema"]["schema"]

    assert schema["type"] == "object"
    assert "anyOf" not in schema


def test_decision_is_a_plain_top_level_string_never_a_nested_object() -> None:
    """The exact defect the M7.1h smoke test surfaced: Groq/`openai/gpt-
    oss-20b` did not reliably produce the old shared contract's nested
    `{"decision": {...}}` envelope. The new schema must not even offer that
    shape — `decision` is a plain enum-of-strings property directly on the
    root object."""
    schema = _captured_schema()["json_schema"]["schema"]
    decision_schema = schema["properties"]["decision"]

    assert decision_schema["type"] == "string"
    assert "anyOf" not in decision_schema
    assert "$ref" not in decision_schema
    assert set(decision_schema["enum"]) == {"no_action", "suggest_send_email"}


def test_reason_and_confidence_are_direct_siblings_of_decision() -> None:
    """Every field is flat at the root — never nested under `decision` or
    any other wrapper."""
    schema = _captured_schema()["json_schema"]["schema"]
    properties = schema["properties"]

    assert set(properties) == {"decision", "reason", "confidence", "summary", "payload", "evidence"}
    assert properties["reason"]["type"] == "string"
    assert properties["confidence"]["type"] == "number"


def test_no_defs_contains_a_second_decision_discriminator() -> None:
    """There is no `$defs._WireNoAction`/`$defs._WireSendEmailSuggestion`
    (or equivalent) hiding a second `decision` field anywhere — the flat
    contract has exactly one `decision` property, on the root."""
    schema = _captured_schema()["json_schema"]["schema"]
    decision_occurrences = _collect_values(schema, keys=("title",))
    assert "_WireNoAction" not in decision_occurrences
    assert "_WireSendEmailSuggestion" not in decision_occurrences
    assert "_WireReasoningResponse" not in decision_occurrences


# ---------------------------------------------------------------------------
# D. closed decision set / forbidden action types
# ---------------------------------------------------------------------------


def test_create_task_and_update_opportunity_stage_have_no_schema_representation() -> None:
    schema = _captured_schema()["json_schema"]["schema"]
    selectable_values = _collect_values(schema, keys=("const", "enum", "default"))

    assert "create_task" not in selectable_values
    assert "update_opportunity_stage" not in selectable_values
    assert selectable_values & {"no_action", "suggest_send_email"} == {
        "no_action",
        "suggest_send_email",
    }


def test_no_action_type_field_exists_the_discriminator_alone_selects_send_email() -> None:
    schema = _captured_schema()["json_schema"]["schema"]

    assert "action_type" not in schema["properties"]


# ---------------------------------------------------------------------------
# E. SEND_EMAIL payload / evidence bounds, preserved from the shared contract
# ---------------------------------------------------------------------------


def test_payload_subject_and_body_are_required_when_present_no_recipients() -> None:
    """M7.1l.1: the model drafts subject/body only — there is no
    `recipients` field anywhere in the payload schema sent to Groq; the
    trusted recipient is assembled by Nerva afterward, outside this schema
    entirely (see `_groq_send_email_wire`'s own docstring)."""
    schema = _captured_schema()["json_schema"]["schema"]
    payload_ref = schema["properties"]["payload"]["anyOf"][0]
    payload = schema["$defs"][payload_ref["$ref"].rsplit("/", 1)[-1]]

    assert set(payload["properties"]) == {"subject", "body"}
    assert "recipients" not in payload["properties"]
    assert set(payload["required"]) == {"subject", "body"}


def test_evidence_is_a_bounded_array_not_unconstrained() -> None:
    schema = _captured_schema()["json_schema"]["schema"]

    evidence_variants = schema["properties"]["evidence"]["anyOf"]
    evidence = next(v for v in evidence_variants if v.get("type") == "array")
    assert evidence["minItems"] == 1
    assert evidence["maxItems"] == 10


def test_payload_and_evidence_are_nullable_not_whole_object_union_branches() -> None:
    """The specific, deliberately narrow use of `anyOf` this contract makes:
    an optional property's `anyOf` is exactly two branches, one of them
    literally `{"type": "null"}` — never two different *populated* object
    shapes to choose between."""
    schema = _captured_schema()["json_schema"]["schema"]

    for field in ("summary", "payload", "evidence"):
        variants = schema["properties"][field]["anyOf"]
        assert len(variants) == 2
        assert {"type": "null"} in variants
