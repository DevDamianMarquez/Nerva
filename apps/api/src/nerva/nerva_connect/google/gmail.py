"""A thin client for the parts of the Gmail REST API Nerva Connect needs.

Calls the official Gmail API directly over HTTPS (never a scraping/unofficial route).
Only read endpoints are used: `users.messages.list` and `users.messages.get`. Nothing
here can send, modify, label or delete anything in the connected mailbox.
"""

from datetime import date
from typing import Any

import httpx

from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError

API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_TIMEOUT = httpx.Timeout(15.0)

type RawGmailMessage = dict[str, Any]


class GmailClient:
    """The real implementation. Tests use a fake with the same method signatures
    (see `apps/api/tests/fakes/google.py`) — never a real mailbox."""

    def list_recent_message_ids(
        self, access_token: str, *, after: date, max_results: int
    ) -> list[str]:
        """IDs of messages received on or after `after`, most recent first, capped at
        `max_results` regardless of how many the mailbox actually has — the sync
        bounds documented in docs/ARCHITECTURE.md."""
        ids: list[str] = []
        page_token: str | None = None
        query = f"after:{after.strftime('%Y/%m/%d')}"
        while len(ids) < max_results:
            params: dict[str, str | int] = {
                "q": query,
                "maxResults": min(100, max_results - len(ids)),
            }
            if page_token:
                params["pageToken"] = page_token
            body = self._get("/messages", access_token, params=params)
            ids.extend(message["id"] for message in body.get("messages", []))
            page_token = body.get("nextPageToken")
            if not page_token:
                break
        return ids[:max_results]

    def get_message(self, access_token: str, message_id: str) -> RawGmailMessage:
        return self._get(f"/messages/{message_id}", access_token, params={"format": "full"})

    def get_profile(self, access_token: str) -> dict[str, Any]:
        return self._get("/profile", access_token, params={})

    def _get(self, path: str, access_token: str, *, params: dict[str, str | int]) -> dict[str, Any]:
        try:
            response = httpx.get(
                f"{API_BASE}{path}",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise GoogleApiError(f"Could not reach the Gmail API: {exc}") from exc

        if response.status_code == 401:
            raise GoogleAuthError("Gmail rejected the access token.")
        if response.status_code != 200:
            raise GoogleApiError(f"Gmail API returned {response.status_code} for {path}.")

        data: dict[str, Any] = response.json()
        return data
