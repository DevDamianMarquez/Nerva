"""The Google OAuth connect flow: initiation, state validation, and the callback.

No real network call or Google account is involved — `google_oauth_client` (the
`FakeGoogleOAuthClient` fixture) stands in for Google's endpoints.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from nerva.api.deps import get_google_oauth_client
from nerva.core.config import Settings
from nerva.core.security import hash_session_token
from nerva.models import OAuthState
from nerva.nerva_connect.google.oauth import GoogleOAuthClient
from tests.fakes.google import FakeGoogleOAuthClient
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


def _extract_state(authorization_url: str) -> str:
    query = parse_qs(urlparse(authorization_url).query)
    return query["state"][0]


def test_authenticated_owner_can_start_the_connect_flow(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )

    assert response.status_code == 200
    assert response.json()["authorization_url"].startswith("https://accounts.google.com/")
    assert _extract_state(response.json()["authorization_url"])


def test_unauthenticated_connect_is_rejected(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    response = client.post(f"/v1/workspaces/{workspace_id}/integrations/google/connect")

    assert response.status_code == 401


def test_connect_without_google_credentials_configured_returns_503(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    google_oauth_client: FakeGoogleOAuthClient,
) -> None:
    """The real `GoogleOAuthClient` (not the fake) refuses to build an authorization
    URL when `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET` are blank. The route must
    translate that into the project's normal error shape instead of an unhandled 500.
    Credentials are forced blank here (rather than relying on `.env`) so this test
    passes regardless of whether a developer has configured real Google OAuth
    credentials locally to verify the live Gmail flow."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    real_oauth_client = GoogleOAuthClient(
        Settings(environment="test", google_client_id="", google_client_secret="")
    )
    client.app.dependency_overrides[get_google_oauth_client] = lambda: real_oauth_client  # type: ignore[attr-defined]
    try:
        response = client.post(
            f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
        )
    finally:
        client.app.dependency_overrides[get_google_oauth_client] = lambda: google_oauth_client  # type: ignore[attr-defined]

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"


def test_a_non_owner_member_cannot_start_the_connect_flow(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    # Bob is not a member at all here, so this also exercises the "not a member"
    # (404, tenant-hiding) path rather than "member but not owner" — see the
    # dedicated non-owner test in test_integrations_management.py for that case.
    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=member.cookies
    )

    assert response.status_code == 404


def test_the_callback_completes_the_connection(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    google_oauth_client: FakeGoogleOAuthClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = _extract_state(begin.json()["authorization_url"])

    callback = client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert callback.headers["location"] == "http://127.0.0.1:3000/settings?integration=connected"
    integrations = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()
    assert len(integrations) == 1
    assert integrations[0]["external_account_email"] == google_oauth_client.identity.email
    assert integrations[0]["status"] == "connected"


def test_google_denying_consent_redirects_with_an_error(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = _extract_state(begin.json()["authorization_url"])

    callback = client.get(
        "/v1/integrations/google/callback",
        params={"error": "access_denied", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert "integration=error" in callback.headers["location"]
    listing = client.get(f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies)
    assert listing.json() == []


@pytest.mark.parametrize(
    "bad_state",
    ["not-a-real-state-value", ""],
    ids=["unknown", "missing"],
)
def test_an_invalid_state_is_rejected(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace, bad_state: str
) -> None:
    owner = make_user("Alice")
    make_workspace(owner)

    params = {"code": "fake-auth-code"}
    if bad_state:
        params["state"] = bad_state
    callback = client.get(
        "/v1/integrations/google/callback",
        params=params,
        cookies=owner.cookies,
        follow_redirects=False,
    )

    assert callback.status_code == 302
    assert "integration=error" in callback.headers["location"]


def test_a_state_cannot_be_reused(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = _extract_state(begin.json()["authorization_url"])
    first = client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )
    assert "integration=connected" in first.headers["location"]

    replay = client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )

    assert "integration=error" in replay.headers["location"]
    assert "reason=invalid_state" in replay.headers["location"]


def test_a_state_issued_for_one_user_cannot_be_completed_by_another(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner, other = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = _extract_state(begin.json()["authorization_url"])

    callback = client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=other.cookies,
        follow_redirects=False,
    )

    assert "integration=error" in callback.headers["location"]
    assert "reason=state_user_mismatch" in callback.headers["location"]
    listing = client.get(f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies)
    assert listing.json() == []


def test_an_expired_state_is_rejected(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace, db_session: Session
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = _extract_state(begin.json()["authorization_url"])
    record = db_session.scalars(
        select(OAuthState).where(OAuthState.state_hash == hash_session_token(state))
    ).one()
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    callback = client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )

    assert "integration=error" in callback.headers["location"]
    assert "reason=invalid_state" in callback.headers["location"]


def test_reconnecting_the_same_google_account_updates_the_existing_integration(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    def connect() -> None:
        begin = client.post(
            f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
        )
        state = _extract_state(begin.json()["authorization_url"])
        client.get(
            "/v1/integrations/google/callback",
            params={"code": "fake-auth-code", "state": state},
            cookies=owner.cookies,
            follow_redirects=False,
        )

    connect()
    connect()

    integrations = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()
    assert len(integrations) == 1
