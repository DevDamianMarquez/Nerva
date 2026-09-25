"""`nerva_ai.prompts`: the real prompt content sent to a provider (Block 7).
Checks the two properties Block 7 explicitly requires — prompt-injection defense
and "unknown/null over fabrication" — are actually present in `SYSTEM_PROMPT`,
plus that `build_user_prompt` renders a conversation's content correctly. Never
calls a provider."""

from datetime import UTC, datetime

from nerva.core.enums import MessageDirection
from nerva.nerva_ai.client import AnalysisInput, AnalysisMessage
from nerva.nerva_ai.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt


def test_system_prompt_instructs_the_model_to_ignore_embedded_instructions() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "untrusted" in lowered
    assert "ignore" in lowered
    assert "instructions" in lowered


def test_system_prompt_instructs_null_over_fabrication() -> None:
    lowered = SYSTEM_PROMPT.lower()
    assert "null" in lowered
    assert "fabricate" in lowered or "invent" in lowered


def test_prompt_version_is_bumped_from_the_placeholder() -> None:
    assert PROMPT_VERSION != "v1"


def test_build_user_prompt_includes_the_subject() -> None:
    prompt = build_user_prompt(AnalysisInput(subject="Pricing for 3 seats", messages=[]))

    assert "Pricing for 3 seats" in prompt


def test_build_user_prompt_uses_a_fallback_for_a_missing_subject() -> None:
    prompt = build_user_prompt(AnalysisInput(subject=None, messages=[]))

    assert "(no subject)" in prompt


def test_build_user_prompt_includes_every_message_in_order() -> None:
    request = AnalysisInput(
        subject="Pricing for 3 seats",
        messages=[
            AnalysisMessage(
                direction=MessageDirection.INBOUND,
                sender_label="Priya Nair",
                sent_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                body_text="Can you send pricing for 3 seats?",
            ),
            AnalysisMessage(
                direction=MessageDirection.OUTBOUND,
                sender_label="You",
                sent_at=datetime(2026, 1, 15, 13, 0, tzinfo=UTC),
                body_text="Sure, here is a quote.",
            ),
        ],
    )

    prompt = build_user_prompt(request)

    first_index = prompt.index("Can you send pricing for 3 seats?")
    second_index = prompt.index("Sure, here is a quote.")
    assert first_index < second_index
    assert "Priya Nair" in prompt
    assert "inbound" in prompt
    assert "outbound" in prompt
    assert "2026-01-15T12:00:00+00:00" in prompt
