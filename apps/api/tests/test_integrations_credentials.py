"""Credential storage: encryption at rest, API exposure, and Google rejecting a
refresh token."""

from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from nerva.core.config import Settings
from nerva.core.crypto import EncryptionNotConfigured, decrypt_secret, encrypt_secret
from nerva.models import Integration
from tests.fakes.google import FakeGoogleOAuthClient
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


def _connect_google(client: TestClient, owner: Account, workspace_id: str) -> None:
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = parse_qs(urlparse(begin.json()["authorization_url"]).query)["state"][0]
    callback = client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )
    assert "integration=connected" in callback.headers["location"], callback.headers["location"]


def test_the_refresh_token_is_encrypted_at_rest(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace, db_session: Session
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _connect_google(client, owner, workspace_id)

    integration = db_session.scalars(select(Integration)).one()

    assert integration.encrypted_refresh_token is not None
    assert integration.encrypted_refresh_token != "fake-refresh-token"
    assert decrypt_secret(integration.encrypted_refresh_token) == "fake-refresh-token"


def test_the_api_never_returns_a_token_field(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _connect_google(client, owner, workspace_id)

    response = client.get(f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies)
    body = response.text.lower()

    assert "refresh_token" not in body
    assert "access_token" not in body
    assert "fake-refresh-token" not in body


def test_encrypting_without_a_configured_key_fails_clearly() -> None:
    settings = Settings(environment="test", integration_encryption_key="")

    with pytest.raises(EncryptionNotConfigured):
        encrypt_secret("a-secret", settings=settings)


def test_decrypting_with_the_wrong_key_fails_clearly() -> None:
    encrypted = encrypt_secret("a-secret", settings=Settings(environment="test"))
    wrong_key = "wpJn3fSJvB3PsCe6vwm23fJdw2Kdz8bxUmFHZAF3IY0="
    wrong_key_settings = Settings(environment="test", integration_encryption_key=wrong_key)

    with pytest.raises(EncryptionNotConfigured):
        decrypt_secret(encrypted, settings=wrong_key_settings)


def test_a_revoked_refresh_token_marks_the_integration_needs_reauth(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    google_oauth_client: FakeGoogleOAuthClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _connect_google(client, owner, workspace_id)
    integration_id = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()[0]["id"]
    google_oauth_client.fail_refresh = True

    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/sync", cookies=owner.cookies
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"code": "needs_reauth"}
    integration = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()[0]
    assert integration["status"] == "needs_reauth"
