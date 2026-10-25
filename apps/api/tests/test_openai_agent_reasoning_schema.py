"""Structured-output schema validation for `OpenAIAgentReasoningProvider`'s
private wire schema (M7.1e) — no network call, no OpenAI credentials.

Generates the *actual* JSON Schema the `openai` SDK would send as
`response_format` (via its own `type_to_response_format_param` helper — the
exact function `.chat.completions.parse` calls internally) and inspects it
structurally, so a regression here (e.g. a future field reintroducing a
free-form `dict`) is caught before it could ever reach a real `strict`
Structured Outputs request and be rejected by OpenAI's API.

This module tests the *shape* of what OpenAI is asked to return — it proves
nothing about `nerva_core.proposal_policy`, which remains the unconditional
authority on whether a validated decision is actually safe to act on (see
`nerva_ai.providers.openai_agent_reasoning`'s own module docstring)."""

from typing import Any

from openai.lib._parsing._completions import type_to_response_format_param

from nerva.nerva_ai.providers._send_email_wire import _WireReasoningResponse


def _generate_schema() -> dict[str, Any]:
    response_format = type_to_response_format_param(_WireReasoningResponse)
    assert isinstance(response_format, dict)
    json_schema = response_format["json_schema"]
    assert json_schema["strict"] is True
    schema = json_schema["schema"]
    assert isinstance(schema, dict)
    return schema


def _all_object_schemas(node: object) -> list[dict[str, Any]]:
    """Every dict in the schema tree whose own `"type"` is `"object"` —
    walks `$defs` and nested `properties`/`items` alike, so a schema change
    that adds a new nested object anywhere is covered automatically, not
    just the ones this test happens to name today."""
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


def test_every_object_schema_sets_additional_properties_false() -> None:
    """OpenAI's `strict` Structured Outputs mode requires exactly this on
    every object in the schema — the one constraint that broke when
    `ActionSuggestion.payload: dict[str, JsonValue]` was tried directly (see
    the provider module's own docstring for the schema that produced)."""
    schema = _generate_schema()
    objects = _all_object_schemas(schema)

    assert objects, "expected at least one object schema"
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj


def test_no_free_form_json_value_object_exists_anywhere() -> None:
    """The specific shape that made the original `ActionSuggestion` schema
    unusable: an object whose `additionalProperties` is itself a schema
    (accepting arbitrary keys/values) rather than the literal `False`."""
    schema = _generate_schema()
    objects = _all_object_schemas(schema)

    for obj in objects:
        additional = obj.get("additionalProperties")
        assert additional is False or additional is None, (
            f"found a non-strict additionalProperties value: {additional!r}"
        )
    # And, structurally, the one historically-dangerous name is simply gone.
    assert "JsonValue" not in str(schema)


def test_the_decision_union_is_closed_to_exactly_two_variants() -> None:
    schema = _generate_schema()
    decision_schema = schema["properties"]["decision"]
    variants = decision_schema["anyOf"]

    assert len(variants) == 2
    ref_names = {variant["$ref"].rsplit("/", 1)[-1] for variant in variants}
    assert ref_names == {"_WireNoAction", "_WireSendEmailSuggestion"}


def test_no_action_type_field_exists_the_discriminator_alone_selects_send_email() -> None:
    """There is no `action_type` property anywhere a model could set to
    something other than what the discriminator itself already names —
    confirmed structurally, not merely by prompt instruction."""
    schema = _generate_schema()
    defs = schema["$defs"]

    assert "action_type" not in defs["_WireSendEmailSuggestion"]["properties"]
    discriminator = defs["_WireSendEmailSuggestion"]["properties"]["decision"]
    assert discriminator["const"] == "suggest_send_email"


def test_create_task_and_update_opportunity_stage_select_no_schema_branch() -> None:
    """Every `const`/`enum`/`default` value anywhere in the schema — the only
    places a structured-output grammar actually constrains a selectable
    value — excludes both forbidden type names. (Free text in a `description`
    field, which does not constrain generation, is deliberately not checked
    here — see `test_openai_agent_reasoning_provider.py`'s own version of
    this test for why a naive full-string search is the wrong check.)"""
    schema = _generate_schema()
    selectable_values = _collect_values(schema, keys=("const", "enum", "default"))

    assert "create_task" not in selectable_values
    assert "update_opportunity_stage" not in selectable_values


def test_required_lists_match_declared_properties_exactly() -> None:
    """OpenAI's `strict` mode requires every declared property to be listed
    in `required` (optionality is expressed via `anyOf [type, null]`
    instead, never by omitting a key from `required`) — confirmed for every
    object in the schema, not just the ones this test names by hand."""
    schema = _generate_schema()
    for obj in _all_object_schemas(schema):
        properties = set(obj.get("properties", {}))
        if not properties:
            continue
        assert set(obj.get("required", [])) == properties, obj


def test_evidence_is_a_bounded_array_not_unconstrained() -> None:
    schema = _generate_schema()
    suggestion = schema["$defs"]["_WireSendEmailSuggestion"]
    evidence = suggestion["properties"]["evidence"]
    assert evidence["type"] == "array"
    assert evidence["minItems"] == 1
    assert evidence["maxItems"] == 10


def test_send_email_payload_has_only_subject_and_body_no_recipients() -> None:
    """M7.1l.1: the model drafts subject/body only — the trusted recipient
    is assembled by Nerva afterward, outside this schema entirely (see
    `_send_email_wire._WireSendEmailPayload`'s own docstring)."""
    schema = _generate_schema()
    payload = schema["$defs"]["_WireSendEmailPayload"]

    assert set(payload["properties"]) == {"subject", "body"}
    assert "recipients" not in payload["properties"]
    assert set(payload["required"]) == {"subject", "body"}


def test_subject_and_body_bounds_are_unchanged() -> None:
    schema = _generate_schema()
    payload = schema["$defs"]["_WireSendEmailPayload"]

    assert payload["properties"]["subject"]["minLength"] == 1
    assert payload["properties"]["subject"]["maxLength"] == 200
    assert payload["properties"]["body"]["minLength"] == 1
    assert payload["properties"]["body"]["maxLength"] == 20_000


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
