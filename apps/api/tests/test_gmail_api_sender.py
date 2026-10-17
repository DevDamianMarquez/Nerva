"""Milestone 5 Block 5 correction: isolated tests for `GmailAPISender`, the real
`GmailSender` implementation (`nerva.nerva_connect.google.sender`).

Every other test touching Gmail `send_email` (`test_gmail_send_executor.py`,
`test_gmail_send_boundary.py`) substitutes `tests.fakes.google.FakeGmailSender` and
never constructs this class, so none of them exercise its actual request
construction or response handling. This file does, against a mocked `httpx.post` —
no test here opens a socket or reaches a real Google endpoint.
"""

import base64
import socket
from collections.abc import Callable
from email import policy
from email.parser import BytesParser

import httpx
import pytest

from nerva.nerva_connect.google.errors import (
    GoogleApiError,
    GoogleApiErrorCategory,
    GoogleAuthError,
)
from nerva.nerva_connect.google.gmail import API_BASE
from nerva.nerva_connect.google.sender import GmailAPISender, GmailSendResult

SEND_URL = f"{API_BASE}/messages/send"

FakePost = Callable[..., httpx.Response]


def _install(monkeypatch: pytest.MonkeyPatch, fake_post: FakePost) -> None:
    monkeypatch.setattr(httpx, "post", fake_post)


def _ok_response(
    *, message_id: str = "msg_1", thread_id: str | None = "thread_1"
) -> httpx.Response:
    body: dict[str, object] = {"id": message_id}
    if thread_id is not None:
        body["threadId"] = thread_id
    return httpx.Response(200, json=body)


@pytest.fixture
def sender() -> GmailAPISender:
    return GmailAPISender()


# ---------------------------------------------------------------------------
# A. Request shape: official endpoint, MIME construction, base64url encoding
# ---------------------------------------------------------------------------


def test_posts_to_the_official_messages_send_endpoint(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    calls = []

    def fake_post(url: str, **kwargs: object) -> httpx.Response:
        calls.append((url, kwargs))
        return _ok_response()

    _install(monkeypatch, fake_post)

    sender.send_email(
        "token-abc", recipients=["priya@halcyon.example"], subject="Hi", body="body text"
    )

    assert len(calls) == 1
    url, kwargs = calls[0]
    assert url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    assert kwargs["headers"] == {"Authorization": "Bearer token-abc"}


def test_the_raw_field_is_a_base64url_encoded_rfc5322_message(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    sent_json: dict[str, object] = {}

    def fake_post(url: str, *, json: dict[str, object], **kwargs: object) -> httpx.Response:
        sent_json.update(json)
        return _ok_response()

    _install(monkeypatch, fake_post)

    sender.send_email(
        "token",
        recipients=["priya@halcyon.example", "amir@halcyon.example"],
        subject="Pricing for 3 seats",
        body="Hi Priya,\n\nFollowing up.",
    )

    raw = sent_json["raw"]
    assert isinstance(raw, str)
    # Base64url never contains '+' or '/' (standard base64's alphabet does) — a
    # regression to plain `base64.b64encode` would be visible here.
    assert "+" not in raw
    assert "/" not in raw

    decoded = base64.urlsafe_b64decode(raw)
    parsed = BytesParser(policy=policy.default).parsebytes(decoded)
    assert parsed["To"] == "priya@halcyon.example, amir@halcyon.example"
    assert parsed["Subject"] == "Pricing for 3 seats"
    assert parsed.get_content().strip() == "Hi Priya,\n\nFollowing up."


def test_no_real_socket_is_ever_opened(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("GmailAPISender must never open a socket in tests")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok_response())

    result = sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert result.message_id == "msg_1"


# ---------------------------------------------------------------------------
# B. Successful responses
# ---------------------------------------------------------------------------


def test_a_successful_response_returns_the_message_and_thread_id(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: _ok_response(message_id="18c9f2b1", thread_id="18c9f2aa"))

    result = sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert result == GmailSendResult(message_id="18c9f2b1", thread_id="18c9f2aa")


def test_a_response_without_a_thread_id_leaves_thread_id_as_none_not_fabricated(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    """Gmail's response omitted `threadId` — the result must say so (`None`), not
    invent one by reusing `message_id` (see `GmailSendResult`'s docstring)."""
    _install(monkeypatch, lambda *a, **k: _ok_response(message_id="msg_only", thread_id=None))

    result = sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert result == GmailSendResult(message_id="msg_only", thread_id=None)


# ---------------------------------------------------------------------------
# C. Malformed / missing provider responses (HTTP 200, bad body)
# ---------------------------------------------------------------------------


def test_a_non_json_200_response_is_a_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(200, content=b"<html>not json</html>"))

    with pytest.raises(GoogleApiError) as excinfo:
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert "<html>" not in str(excinfo.value)


def test_an_empty_200_response_is_a_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(200, content=b""))

    with pytest.raises(GoogleApiError):
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")


def test_a_200_response_that_is_a_json_array_not_an_object_is_rejected(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(200, json=["unexpected", "array"]))

    with pytest.raises(GoogleApiError):
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")


def test_a_200_response_missing_a_message_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(200, json={"threadId": "t1"}))

    with pytest.raises(GoogleApiError, match="message id"):
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")


def test_a_200_response_with_a_blank_message_id_is_rejected(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(200, json={"id": ""}))

    with pytest.raises(GoogleApiError, match="message id"):
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")


# ---------------------------------------------------------------------------
# D. HTTP failures
# ---------------------------------------------------------------------------


def test_a_401_response_raises_google_auth_error(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(401, json={"error": "invalid_token"}))

    with pytest.raises(GoogleAuthError):
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")


def test_a_429_response_is_rate_limited_with_retry_after(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(
        monkeypatch,
        lambda *a, **k: httpx.Response(
            429, json={"error": "quota"}, headers={"Retry-After": "17"}
        ),
    )

    with pytest.raises(GoogleApiError) as excinfo:
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    error = excinfo.value
    assert error.category is GoogleApiErrorCategory.RATE_LIMIT
    assert error.retry_after == 17.0
    assert "quota" not in str(error)


def test_a_429_response_without_retry_after_still_classifies_as_rate_limited(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(429, json={"error": "quota"}))

    with pytest.raises(GoogleApiError) as excinfo:
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert excinfo.value.retry_after is None


@pytest.mark.parametrize("status_code", [400, 403, 404])
def test_a_4xx_response_is_a_client_error_and_never_leaks_the_body(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender, status_code: int
) -> None:
    _install(
        monkeypatch,
        lambda *a, **k: httpx.Response(
            status_code, json={"error": {"message": "invalid recipient <secret/>"}}
        ),
    )

    with pytest.raises(GoogleApiError) as excinfo:
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert excinfo.value.category is GoogleApiErrorCategory.CLIENT_ERROR
    assert "<secret/>" not in str(excinfo.value)


@pytest.mark.parametrize("status_code", [500, 503])
def test_a_5xx_response_is_a_server_error(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender, status_code: int
) -> None:
    _install(monkeypatch, lambda *a, **k: httpx.Response(status_code, content=b"outage"))

    with pytest.raises(GoogleApiError) as excinfo:
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert excinfo.value.category is GoogleApiErrorCategory.SERVER_ERROR


# ---------------------------------------------------------------------------
# E. Transport failures (never reached an HTTP response at all)
# ---------------------------------------------------------------------------


def test_a_connection_failure_is_a_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("Connection refused")

    _install(monkeypatch, fake_post)

    with pytest.raises(GoogleApiError) as excinfo:
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")

    assert excinfo.value.category is GoogleApiErrorCategory.UNKNOWN
    assert excinfo.value.status_code is None


def test_a_timeout_is_a_safe_provider_error(
    monkeypatch: pytest.MonkeyPatch, sender: GmailAPISender
) -> None:
    def fake_post(*args: object, **kwargs: object) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    _install(monkeypatch, fake_post)

    with pytest.raises(GoogleApiError):
        sender.send_email("token", recipients=["a@b.example"], subject="s", body="b")
