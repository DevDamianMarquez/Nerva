"""Deterministic, conservative reduction of quoted reply history in an email body.

Gmail (and most mail clients) repeats the entire prior thread at the bottom of
every reply. That history already exists as separate `Message` rows in the same
`Conversation`, so re-sending it to a model on every message wastes tokens and
dilutes the one part of the text that is actually new. This module trims that
trailing block, but only when it recognizes a specific, conventional boundary
marker — never by guessing. If no such marker is found, or if trimming would
discard nearly everything (a pure forward with no added comment), the original
text is returned unchanged. Only known English-language conventions are
recognized in this version; anything else is left alone, which is the safe
direction to be wrong in.
"""

import re

# If trimming would leave less than this much non-whitespace content, the
# boundary was probably a false positive (or the whole message really is just a
# forward) — better to send the untrimmed original than send almost nothing.
_MIN_KEPT_CONTENT_CHARS = 10

# How many consecutive `>`-prefixed lines are required before treating the first
# one as the start of a quoted block, so a single stray "> 3" in a sentence about
# comparisons cannot truncate an entire message.
_MIN_CONSECUTIVE_QUOTE_LINES = 2

_ON_WROTE_RE = re.compile(r"^on .{1,120} wrote:\s*$", re.IGNORECASE)
_ORIGINAL_MESSAGE_RE = re.compile(r"^-{2,}\s*original message\s*-{2,}$", re.IGNORECASE)
_FORWARDED_MESSAGE_RE = re.compile(r"^-{2,}\s*forwarded message\s*-{2,}$", re.IGNORECASE)
_LONG_SEPARATOR_RE = re.compile(r"^[_-]{10,}$")
_QUOTE_PREFIX_RE = re.compile(r"^\s*>")
_HEADER_FROM_RE = re.compile(r"^from:\s", re.IGNORECASE)
_HEADER_SENT_OR_DATE_RE = re.compile(r"^(sent|date):\s", re.IGNORECASE)


def strip_quoted_reply(text: str) -> str:
    """Returns `text` with a trailing quoted-reply block removed, if (and only
    if) a recognized boundary is found and enough content remains before it."""
    lines = text.splitlines()
    boundary = _find_boundary(lines)
    if boundary is None:
        return text

    kept = "\n".join(lines[:boundary]).rstrip()
    if len(kept.strip()) < _MIN_KEPT_CONTENT_CHARS:
        return text
    return kept


def _find_boundary(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        if (
            _ON_WROTE_RE.match(stripped)
            or _ORIGINAL_MESSAGE_RE.match(stripped)
            or _FORWARDED_MESSAGE_RE.match(stripped)
            or _LONG_SEPARATOR_RE.match(stripped)
        ):
            return index
        if _HEADER_FROM_RE.match(stripped) and _next_nonblank_matches(
            lines, index + 1, _HEADER_SENT_OR_DATE_RE
        ):
            return index
        if _QUOTE_PREFIX_RE.match(line) and _starts_quote_run(lines, index):
            return index
    return None


def _next_nonblank_matches(lines: list[str], start: int, pattern: re.Pattern[str]) -> bool:
    for line in lines[start : start + 3]:
        stripped = line.strip()
        if stripped:
            return bool(pattern.match(stripped))
    return False


def _starts_quote_run(lines: list[str], start: int) -> bool:
    window = lines[start : start + _MIN_CONSECUTIVE_QUOTE_LINES]
    return len(window) == _MIN_CONSECUTIVE_QUOTE_LINES and all(
        _QUOTE_PREFIX_RE.match(line) for line in window
    )
