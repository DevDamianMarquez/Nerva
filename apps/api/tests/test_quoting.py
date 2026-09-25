"""`nerva.nerva_ai.quoting.strip_quoted_reply`: conservative removal of trailing
quoted-reply history. Every case here should be read as "this exact conventional
marker, and nothing looser" — the whole point is to never risk cutting a sender's
own words because of a coincidental resemblance."""

from nerva.nerva_ai.quoting import strip_quoted_reply


def test_text_with_no_quote_marker_is_returned_unchanged() -> None:
    text = "Hi Alex,\n\nCan you send pricing for 3 seats?\n\nThanks,\nPriya"

    assert strip_quoted_reply(text) == text


def test_trims_the_classic_on_date_name_wrote_marker() -> None:
    text = (
        "Sure, next Tuesday works.\n\n"
        "On Mon, Jan 5, 2026 at 3:00 PM Priya Nair <priya@halcyon.example> wrote:\n"
        "> Can we meet next week?\n"
        "> Let me know what works.\n"
    )

    assert strip_quoted_reply(text) == "Sure, next Tuesday works."


def test_trims_outlook_style_original_message_separator() -> None:
    text = (
        "Approved, go ahead.\n\n"
        "-----Original Message-----\n"
        "From: Priya Nair\n"
        "Sent: Monday, January 5, 2026 3:00 PM\n"
        "To: Alex\n"
        "Subject: Approval needed\n\n"
        "Can you approve this?\n"
    )

    assert strip_quoted_reply(text) == "Approved, go ahead."


def test_trims_gmail_style_forwarded_message_separator() -> None:
    text = (
        "FYI, see below.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Someone <someone@example.com>\n"
        "Date: Mon, Jan 5, 2026\n"
        "Subject: Original subject\n\n"
        "The original content.\n"
    )

    assert strip_quoted_reply(text) == "FYI, see below."


def test_trims_a_long_underscore_or_dash_separator() -> None:
    text = "Looks good to me.\n\n" + "_" * 32 + "\nFrom: someone@example.com\nOriginal text.\n"

    assert strip_quoted_reply(text) == "Looks good to me."


def test_trims_a_from_sent_header_block() -> None:
    text = (
        "Thanks, that works for me.\n\n"
        "From: Priya Nair\n"
        "Sent: Monday, January 5, 2026 3:00 PM\n"
        "To: Alex\n\n"
        "Original message text.\n"
    )

    assert strip_quoted_reply(text) == "Thanks, that works for me."


def test_trims_two_or_more_consecutive_angle_bracket_quoted_lines() -> None:
    text = "Yes, that's right.\n\n> First quoted line\n> Second quoted line\n> Third quoted line\n"

    assert strip_quoted_reply(text) == "Yes, that's right."


def test_a_single_angle_bracket_line_does_not_trigger_trimming() -> None:
    text = "The comparison is x > 5, not x < 5.\n\nLet me know if that's clear."

    assert strip_quoted_reply(text) == text


def test_does_not_trim_when_the_result_would_be_almost_empty() -> None:
    """A pure forward with no added comment: trimming would leave nothing useful,
    so the conservative choice is to keep the (quoted) content rather than send an
    almost-empty message to analysis."""
    text = "Hi\n\nOn Mon, Jan 5, 2026 at 3:00 PM Priya Nair wrote:\n> Some quoted content here.\n"

    assert strip_quoted_reply(text) == text


def test_matching_is_case_insensitive() -> None:
    text = "Confirmed.\n\nON MON, JAN 5, 2026 PRIYA NAIR WROTE:\n> Can we meet?\n"

    assert strip_quoted_reply(text) == "Confirmed."


def test_empty_text_is_returned_unchanged() -> None:
    assert strip_quoted_reply("") == ""
