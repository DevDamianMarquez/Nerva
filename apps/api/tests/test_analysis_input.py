"""`nerva.services.analysis.build_analysis_input` and `compute_fingerprint`: pure
functions, no database. Each takes plain (possibly unpersisted) ORM objects and
returns a value — this is where quote-stripping, the input bounds, and the
idempotency fingerprint are proven correct in isolation, before
`test_analysis_service.py` exercises them through the real service."""

import uuid
from datetime import UTC, datetime, timedelta

from nerva.core.enums import ConversationChannel, MessageDirection
from nerva.models import Conversation, Message
from nerva.nerva_ai.client import AnalysisInput
from nerva.services.analysis import (
    MAX_CHARS_PER_MESSAGE,
    MAX_MESSAGES_PER_ANALYSIS,
    MAX_TOTAL_CHARS,
    build_analysis_input,
    compute_fingerprint,
)

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def _conversation(subject: str | None = "Pricing") -> Conversation:
    return Conversation(
        workspace_id=uuid.uuid4(), channel=ConversationChannel.EMAIL, subject=subject
    )


def _message(
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    sender_email: str = "priya@halcyon.example",
    sender_name: str | None = None,
    body_text: str = "Hi",
    sent_at: datetime = NOW,
) -> Message:
    return Message(
        conversation_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        direction=direction,
        sender_email=sender_email,
        sender_name=sender_name,
        recipients="",
        body_text=body_text,
        sent_at=sent_at,
    )


# --- build_analysis_input: shape and sender labeling --------------------------


def test_carries_the_subject_and_each_messages_direction_and_body() -> None:
    conversation = _conversation(subject="Pricing for 3 seats")
    messages = [
        _message(
            direction=MessageDirection.INBOUND,
            sender_name="Priya Nair",
            body_text="Can you send pricing?",
            sent_at=_at(0),
        ),
        _message(
            direction=MessageDirection.OUTBOUND, body_text="Sure, here it is.", sent_at=_at(5)
        ),
    ]

    result = build_analysis_input(conversation, messages)

    assert result.subject == "Pricing for 3 seats"
    assert [m.body_text for m in result.messages] == ["Can you send pricing?", "Sure, here it is."]
    assert result.messages[0].sender_label == "Priya Nair"
    assert result.messages[1].sender_label == "You"


def test_sender_without_a_display_name_falls_back_to_the_email_address() -> None:
    message = _message(sender_email="priya@halcyon.example", sender_name=None)

    result = build_analysis_input(_conversation(), [message])

    assert result.messages[0].sender_label == "priya@halcyon.example"


def test_outbound_messages_are_always_labeled_you_regardless_of_sender_fields() -> None:
    message = _message(
        direction=MessageDirection.OUTBOUND, sender_email="alex@gmail.example", sender_name="Alex"
    )

    result = build_analysis_input(_conversation(), [message])

    assert result.messages[0].sender_label == "You"


def test_never_mutates_the_stored_message_body() -> None:
    message = _message(body_text="> quoted\n> quoted\nActual reply")
    original = message.body_text

    build_analysis_input(_conversation(), [message])

    assert message.body_text == original


# --- Quote stripping reaches the built input -----------------------------------


def test_quoted_reply_history_is_stripped_before_reaching_analysis_input() -> None:
    message = _message(
        body_text="Sure, works.\n\nOn Mon, Jan 5 Priya wrote:\n> Can we meet?\n> Let me know.\n"
    )

    result = build_analysis_input(_conversation(), [message])

    assert result.messages[0].body_text == "Sure, works."


# --- Per-message character bound ------------------------------------------------


def test_a_message_longer_than_the_per_message_bound_is_truncated_with_a_marker() -> None:
    long_body = "x" * (MAX_CHARS_PER_MESSAGE + 500)

    result = build_analysis_input(_conversation(), [_message(body_text=long_body)])

    body = result.messages[0].body_text
    assert len(body) == MAX_CHARS_PER_MESSAGE + 1  # + the one-character truncation marker
    assert body.endswith("…")
    assert body.startswith("x" * 100)  # the start of the message — not the middle or end — survives


def test_a_message_at_or_under_the_bound_is_not_touched() -> None:
    exact_body = "x" * MAX_CHARS_PER_MESSAGE

    result = build_analysis_input(_conversation(), [_message(body_text=exact_body)])

    assert result.messages[0].body_text == exact_body


# --- Message-count bound: keep the newest ---------------------------------------


def test_more_messages_than_the_count_bound_keeps_only_the_newest() -> None:
    total = MAX_MESSAGES_PER_ANALYSIS + 5
    messages = [_message(body_text=f"message {i}", sent_at=_at(i)) for i in range(total)]

    result = build_analysis_input(_conversation(), messages)

    kept = [m.body_text for m in result.messages]
    assert len(kept) == MAX_MESSAGES_PER_ANALYSIS
    assert kept == [f"message {i}" for i in range(5, total)]
    assert "message 0" not in kept
    assert f"message {total - 1}" in kept  # the newest survives


def test_messages_kept_after_the_count_bound_stay_in_chronological_order() -> None:
    total = MAX_MESSAGES_PER_ANALYSIS + 5
    messages = [_message(body_text=f"message {i}", sent_at=_at(i)) for i in range(total)]

    result = build_analysis_input(_conversation(), messages)

    sent_ats = [m.sent_at for m in result.messages]
    assert sent_ats == sorted(sent_ats)


# --- Total character budget: drop the oldest first -------------------------------


def test_total_character_budget_drops_the_oldest_messages_first() -> None:
    body_len = MAX_CHARS_PER_MESSAGE - 100
    count = MAX_TOTAL_CHARS // body_len + 3
    messages = [
        _message(body_text=f"{i:04d}" + "x" * (body_len - 4), sent_at=_at(i)) for i in range(count)
    ]

    result = build_analysis_input(_conversation(), messages)

    assert sum(len(m.body_text) for m in result.messages) <= MAX_TOTAL_CHARS
    kept_indices = {int(m.body_text[:4]) for m in result.messages}
    assert count - 1 in kept_indices  # the newest message survives
    assert 0 not in kept_indices  # the oldest was dropped to make room


def test_a_single_message_exceeding_the_total_budget_alone_is_still_kept() -> None:
    # Per-message truncation always keeps at most MAX_CHARS_PER_MESSAGE, which is
    # itself below MAX_TOTAL_CHARS, so the one remaining message can never be
    # dropped by the total-budget pass — there would be nothing left to analyze.
    long_body = "x" * (MAX_CHARS_PER_MESSAGE + 1000)
    result = build_analysis_input(_conversation(), [_message(body_text=long_body)])

    assert len(result.messages) == 1


# --- compute_fingerprint: determinism and sensitivity ----------------------------


def _fp(
    analysis_input: AnalysisInput,
    *,
    schema_version: int = 1,
    prompt_version: str = "v1",
    model_id: str = "m1",
) -> str:
    return compute_fingerprint(
        analysis_input,
        schema_version=schema_version,
        prompt_version=prompt_version,
        model_id=model_id,
    )


def test_fingerprint_is_stable_for_identical_input() -> None:
    analysis_input = build_analysis_input(_conversation(), [_message(body_text="Hi", sent_at=NOW)])

    first = _fp(analysis_input)
    second = _fp(analysis_input)

    assert first == second
    assert len(first) == 64  # sha256 hex digest


def test_fingerprint_changes_when_message_content_changes() -> None:
    a = build_analysis_input(_conversation(), [_message(body_text="Hi", sent_at=NOW)])
    b = build_analysis_input(_conversation(), [_message(body_text="Hi there", sent_at=NOW)])

    assert _fp(a) != _fp(b)


def test_fingerprint_changes_when_schema_version_changes() -> None:
    analysis_input = build_analysis_input(_conversation(), [_message(body_text="Hi", sent_at=NOW)])

    assert _fp(analysis_input, schema_version=1) != _fp(analysis_input, schema_version=2)


def test_fingerprint_changes_when_prompt_version_changes() -> None:
    analysis_input = build_analysis_input(_conversation(), [_message(body_text="Hi", sent_at=NOW)])

    assert _fp(analysis_input, prompt_version="v1") != _fp(analysis_input, prompt_version="v2")


def test_fingerprint_changes_when_model_id_changes() -> None:
    analysis_input = build_analysis_input(_conversation(), [_message(body_text="Hi", sent_at=NOW)])

    assert _fp(analysis_input, model_id="model-a") != _fp(analysis_input, model_id="model-b")


def test_fingerprint_does_not_depend_on_database_identifiers() -> None:
    """Two conversations/messages with identical content but different (random)
    ids must fingerprint the same — the whole point is that content, not row
    identity, decides whether a cached analysis is still valid."""
    input_one = build_analysis_input(
        _conversation(subject="Pricing"), [_message(body_text="Hi", sent_at=NOW)]
    )
    input_two = build_analysis_input(
        _conversation(subject="Pricing"), [_message(body_text="Hi", sent_at=NOW)]
    )

    fp_one = compute_fingerprint(input_one, schema_version=1, prompt_version="v1", model_id="m1")
    fp_two = compute_fingerprint(input_two, schema_version=1, prompt_version="v1", model_id="m1")

    assert fp_one == fp_two
