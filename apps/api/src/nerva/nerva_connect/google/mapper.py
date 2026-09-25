"""Turns a raw Gmail message (from `gmail.py`) into the plain values
`nerva.services.integrations` needs to write a `Message` row.

Email is untrusted external content. This module never executes or renders HTML: it
only tokenizes it (via the standard library's `html.parser`) to derive safe plain text
for storage and display. The resulting `body_text` is later treated as untrusted input
wherever it reaches an LLM — see docs/ARCHITECTURE.md's content-safety section.
"""

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from typing import Any

from nerva.core.enums import MessageDirection
from nerva.nerva_connect.google.gmail import RawGmailMessage


@dataclass(frozen=True, slots=True)
class NormalizedMessage:
    external_id: str
    thread_external_id: str
    direction: MessageDirection
    sender_email: str
    sender_name: str | None
    recipients: str
    subject: str | None
    body_text: str
    sent_at: datetime


class _TextExtractor(HTMLParser):
    """Collects visible text from HTML, dropping tags, scripts and styles. Does not
    evaluate or render anything — `HTMLParser` only tokenizes."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip_depth += 1
        elif tag in {"br", "p", "div", "tr", "li"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    lines = (line.strip() for line in extractor.text().splitlines())
    return "\n".join(line for line in lines if line).strip()


def _decode_part_body(data: str) -> str:
    # Gmail base64url-encodes part bodies, sometimes without padding.
    padded = data + "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
    except (binascii.Error, ValueError):
        return ""


def extract_plain_text(payload: dict[str, Any]) -> str:
    """Prefers a `text/plain` part anywhere in the (possibly multipart) message;
    falls back to converting the first `text/html` part; falls back to nothing."""
    plain, html = _find_text_parts(payload)
    if plain is not None:
        return plain.strip()
    if html is not None:
        return html_to_text(html)
    return ""


def _find_text_parts(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    mime_type = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data")

    if mime_type == "text/plain" and body_data:
        return _decode_part_body(body_data), None
    if mime_type == "text/html" and body_data:
        return None, _decode_part_body(body_data)

    plain = html = None
    for part in payload.get("parts", []):
        part_plain, part_html = _find_text_parts(part)
        plain = plain or part_plain
        html = html or part_html
        if plain:
            break
    return plain, html


def _header(headers: list[dict[str, str]], name: str) -> str:
    for header in headers:
        if header.get("name", "").lower() == name.lower():
            return header.get("value", "")
    return ""


def parse_gmail_message(raw: RawGmailMessage, *, account_email: str) -> NormalizedMessage:
    payload = raw.get("payload", {})
    headers = payload.get("headers", [])

    sender_name, sender_email = parseaddr(_header(headers, "From"))
    sender_email = sender_email.lower()
    recipients = ", ".join(
        email.lower()
        for _, email in getaddresses(
            [_header(headers, "To"), _header(headers, "Cc")],
        )
        if email
    )

    direction = (
        MessageDirection.OUTBOUND
        if sender_email == account_email.lower()
        else MessageDirection.INBOUND
    )

    internal_date_ms = int(raw.get("internalDate", "0"))
    sent_at = (
        datetime.fromtimestamp(internal_date_ms / 1000, tz=UTC)
        if internal_date_ms
        else datetime.now(UTC)
    )

    body_text = extract_plain_text(payload) or raw.get("snippet", "")

    return NormalizedMessage(
        external_id=raw["id"],
        thread_external_id=raw["threadId"],
        direction=direction,
        sender_email=sender_email,
        sender_name=sender_name or None,
        recipients=recipients,
        subject=_header(headers, "Subject") or None,
        body_text=body_text,
        sent_at=sent_at,
    )
