"""Signup, login, logout and the session that connects them."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from nerva.core.security import SESSION_COOKIE_NAME, hash_session_token
from nerva.models import User, UserSession
from tests.conftest import TEST_PASSWORD
from tests.helpers import Account

MakeUser = Callable[[str], Account]


def test_signup_creates_a_user_and_starts_a_session(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/signup",
        json={"email": "Founder@Example.com", "name": "Founder", "password": TEST_PASSWORD},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "founder@example.com"  # normalized to lower case
    assert "password" not in body
    assert "password_hash" not in body
    token = response.cookies.get(SESSION_COOKIE_NAME)
    assert token

    me = client.get("/v1/auth/me", cookies={SESSION_COOKIE_NAME: token})
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "founder@example.com"
    assert me.json()["workspaces"] == []  # onboarding has not run yet


def test_duplicate_signup_is_a_conflict(client: TestClient) -> None:
    body = {"email": "founder@example.com", "name": "Founder", "password": TEST_PASSWORD}
    client.post("/v1/auth/signup", json=body)
    client.cookies.clear()

    duplicate = client.post(
        "/v1/auth/signup", json={**body, "email": "Founder@Example.com", "name": "Someone Else"}
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


@pytest.mark.parametrize("password", ["short", "x" * 73], ids=["too-short", "too-long"])
def test_signup_rejects_a_password_outside_the_length_policy(
    client: TestClient, password: str
) -> None:
    response = client.post(
        "/v1/auth/signup",
        json={"email": "founder@example.com", "name": "Founder", "password": password},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_signup_rejects_a_malformed_email(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/signup",
        json={"email": "not-an-email", "name": "Founder", "password": TEST_PASSWORD},
    )

    assert response.status_code == 422


def test_the_password_is_hashed_not_stored_in_plain_text(
    client: TestClient, db_session: Session
) -> None:
    client.post(
        "/v1/auth/signup",
        json={"email": "founder@example.com", "name": "Founder", "password": TEST_PASSWORD},
    )

    user = db_session.scalars(select(User).where(User.email == "founder@example.com")).one()

    assert user.password_hash != TEST_PASSWORD
    assert user.password_hash.startswith("$2b$")  # bcrypt


def test_login_succeeds_with_the_right_credentials(client: TestClient, make_user: MakeUser) -> None:
    alice = make_user("Alice")

    response = client.post("/v1/auth/login", json={"email": alice.email, "password": TEST_PASSWORD})

    assert response.status_code == 200
    assert response.json()["id"] == alice.id
    assert response.cookies.get(SESSION_COOKIE_NAME)


def test_login_fails_for_an_unknown_email(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": TEST_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_login_fails_for_the_wrong_password(client: TestClient, make_user: MakeUser) -> None:
    alice = make_user("Alice")

    response = client.post(
        "/v1/auth/login", json={"email": alice.email, "password": "definitely wrong"}
    )

    assert response.status_code == 401


def test_wrong_password_and_unknown_email_give_the_same_message(
    client: TestClient, make_user: MakeUser
) -> None:
    """No user-enumeration signal: a wrong password and an unknown email look identical."""
    alice = make_user("Alice")

    wrong_password = client.post(
        "/v1/auth/login", json={"email": alice.email, "password": "definitely wrong"}
    )
    unknown_email = client.post(
        "/v1/auth/login", json={"email": "nobody@example.com", "password": TEST_PASSWORD}
    )

    assert wrong_password.json()["error"] == unknown_email.json()["error"]


def test_logout_clears_the_session(client: TestClient, make_user: MakeUser) -> None:
    alice = make_user("Alice")

    logout = client.post("/v1/auth/logout", cookies=alice.cookies)
    after = client.get("/v1/auth/me", cookies=alice.cookies)

    assert logout.status_code == 204
    assert after.status_code == 401


def test_logout_without_a_session_still_succeeds(client: TestClient) -> None:
    response = client.post("/v1/auth/logout")

    assert response.status_code == 204


def test_me_requires_authentication(client: TestClient) -> None:
    response = client.get("/v1/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_malformed_session_cookie_is_rejected(client: TestClient) -> None:
    response = client.get("/v1/auth/me", cookies={SESSION_COOKIE_NAME: "not-a-real-token"})

    assert response.status_code == 401


def test_an_expired_session_is_rejected(
    client: TestClient, make_user: MakeUser, db_session: Session
) -> None:
    alice = make_user("Alice")
    record = db_session.scalars(
        select(UserSession).where(UserSession.token_hash == hash_session_token(alice.session_token))
    ).one()
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.flush()

    response = client.get("/v1/auth/me", cookies=alice.cookies)

    assert response.status_code == 401


def test_a_revoked_session_is_rejected(
    client: TestClient, make_user: MakeUser, db_session: Session
) -> None:
    alice = make_user("Alice")
    record = db_session.scalars(
        select(UserSession).where(UserSession.token_hash == hash_session_token(alice.session_token))
    ).one()
    record.revoked_at = datetime.now(UTC)
    db_session.flush()

    response = client.get("/v1/auth/me", cookies=alice.cookies)

    assert response.status_code == 401


def test_update_me_changes_the_display_name(client: TestClient, make_user: MakeUser) -> None:
    alice = make_user("Alice")

    response = client.patch("/v1/auth/me", json={"name": "Alice Chen"}, cookies=alice.cookies)

    assert response.status_code == 200
    assert response.json()["name"] == "Alice Chen"


def test_update_me_without_a_name_is_a_no_op(client: TestClient, make_user: MakeUser) -> None:
    alice = make_user("Alice")

    response = client.patch("/v1/auth/me", json={}, cookies=alice.cookies)

    assert response.status_code == 200
    assert response.json()["name"] == "Alice"


def test_update_me_requires_authentication(client: TestClient) -> None:
    response = client.patch("/v1/auth/me", json={"name": "Nobody"})

    assert response.status_code == 401
