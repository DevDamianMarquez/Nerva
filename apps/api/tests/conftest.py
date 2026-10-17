"""Test fixtures.

Tests run against a real PostgreSQL database (`TEST_DATABASE_URL`, created on demand),
because tenant isolation relies on PostgreSQL features: composite foreign keys, CHECK
constraints and row locks. Each test runs inside a transaction that is rolled back, so
tests never see each other's data.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

import nerva.models  # noqa: F401  (registers tables on Base.metadata)
from nerva.api.deps import (
    get_ai_client,
    get_gmail_client,
    get_gmail_sender,
    get_google_oauth_client,
)
from nerva.app import create_app
from nerva.core.config import Settings, get_settings
from nerva.core.security import SESSION_COOKIE_NAME
from nerva.db.base import Base
from nerva.db.session import get_session
from tests.fakes.ai import FakeAIClient
from tests.fakes.google import FakeGmailClient, FakeGmailSender, FakeGoogleOAuthClient
from tests.helpers import Account, ensure_database

# Satisfies the signup password policy (8-72 characters); not meant to be realistic.
TEST_PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="session")
def test_database_url() -> str:
    url = get_settings().test_database_url
    if not (make_url(url).database or "").endswith("_test"):
        pytest.exit(f"Refusing to run tests against {url!r}: database name must end in _test.")
    return url


@pytest.fixture(scope="session")
def engine(test_database_url: str) -> Iterator[Engine]:
    ensure_database(test_database_url)
    engine = create_engine(test_database_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(engine: Engine) -> Iterator[Session]:
    connection = engine.connect()
    transaction = connection.begin()
    # "create_savepoint": commits made by services release a savepoint instead of
    # committing the outer transaction, which is rolled back at the end of the test.
    session = Session(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def google_oauth_client() -> FakeGoogleOAuthClient:
    """Stands in for Google's OAuth endpoints. Tweak `.fail_exchange` / `.fail_refresh`
    / `.identity` in a test to script a specific scenario."""
    return FakeGoogleOAuthClient()


@pytest.fixture
def gmail_client() -> FakeGmailClient:
    """A canned mailbox. Populate `.messages` before triggering a sync."""
    return FakeGmailClient()


@pytest.fixture
def gmail_sender() -> FakeGmailSender:
    """Stands in for `GmailAPISender`. Set `.next_result` / `.fail_with` in a test to
    script a `send_email` execution outcome. No test using this fixture ever makes a
    real network call or sends a real email — see `tests/fakes/google.py`."""
    return FakeGmailSender()


@pytest.fixture
def ai_client() -> FakeAIClient:
    """Stands in for an LLM provider. Set `.next_result` / `.fail_with` /
    `.model_id` in a test to script a specific scenario. No test using this
    fixture ever makes a real network call — see `tests/fakes/ai.py`."""
    return FakeAIClient()


@pytest.fixture
def client(
    db_session: Session,
    google_oauth_client: FakeGoogleOAuthClient,
    gmail_client: FakeGmailClient,
    gmail_sender: FakeGmailSender,
    ai_client: FakeAIClient,
) -> Iterator[TestClient]:
    app = create_app(Settings(environment="test"))

    def override_session() -> Iterator[Session]:
        try:
            yield db_session
        finally:
            db_session.rollback()  # like the real dependency: discard anything uncommitted

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_google_oauth_client] = lambda: google_oauth_client
    app.dependency_overrides[get_gmail_client] = lambda: gmail_client
    app.dependency_overrides[get_gmail_sender] = lambda: gmail_sender
    app.dependency_overrides[get_ai_client] = lambda: ai_client
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def make_user(client: TestClient) -> Callable[[str], Account]:
    def _make(name: str = "Alice") -> Account:
        email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
        response = client.post(
            "/v1/auth/signup", json={"email": email, "name": name, "password": TEST_PASSWORD}
        )
        assert response.status_code == 201, response.text
        token = response.cookies[SESSION_COOKIE_NAME]
        # Keep the shared client itself anonymous: each account carries its own cookie
        # explicitly, so several logged-in accounts can be used side by side in one test.
        client.cookies.clear()
        return Account(id=response.json()["id"], email=email, session_token=token)

    return _make


@pytest.fixture
def make_workspace(client: TestClient) -> Callable[[Account, str], str]:
    def _make(owner: Account, name: str = "Acme") -> str:
        response = client.post("/v1/workspaces", json={"name": name}, cookies=owner.cookies)
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    return _make


@pytest.fixture
def post(client: TestClient) -> Callable[..., dict[str, Any]]:
    """POST a body as `account` and return the created JSON, asserting success."""

    def _post(account: Account, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = client.post(path, json=body, cookies=account.cookies)
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    return _post
