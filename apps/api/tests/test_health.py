from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from nerva.app import create_app
from nerva.core.config import Settings
from nerva.db.session import get_session


def test_health_reports_ok_and_database_connectivity(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["version"]


class _UnreachableSession:
    def execute(self, *args: object, **kwargs: object) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


def test_health_returns_503_when_database_is_unreachable() -> None:
    app = create_app(Settings(environment="test"))

    def broken_session() -> Iterator[_UnreachableSession]:
        yield _UnreachableSession()

    app.dependency_overrides[get_session] = broken_session

    response = TestClient(app).get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["database"] == "unavailable"


def test_unknown_routes_use_the_standard_error_shape(client: TestClient) -> None:
    response = client.get("/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
