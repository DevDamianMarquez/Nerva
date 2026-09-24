import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import Actor
from nerva.core.enums import ActivityEventType, ActorType
from nerva.core.errors import ImmutableRecordError
from nerva.models import ActivityEvent
from nerva.repositories.workspace_data import ActivityEventRepository
from nerva.services.activity import ActivityService
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


@pytest.fixture
def owner(make_user: MakeUser) -> Account:
    return make_user("Alice")


@pytest.fixture
def workspace_id(make_workspace: MakeWorkspace, owner: Account) -> str:
    return make_workspace(owner, "Northwind")


def test_creating_a_workspace_is_the_first_event(
    client: TestClient, owner: Account, workspace_id: str
) -> None:
    feed = client.get(f"/v1/workspaces/{workspace_id}/activity", cookies=owner.cookies).json()

    assert [event["event_type"] for event in feed] == ["workspace.created"]
    assert feed[0]["actor_type"] == "user"
    assert feed[0]["actor_user_id"] == owner.id
    assert feed[0]["entity_id"] == workspace_id


def test_the_feed_lists_the_newest_events_first(
    client: TestClient, owner: Account, workspace_id: str
) -> None:
    base = f"/v1/workspaces/{workspace_id}"
    client.post(f"{base}/contacts", json={"email": "a@example.com"}, cookies=owner.cookies)
    client.post(f"{base}/opportunities", json={"title": "Deal"}, cookies=owner.cookies)

    feed = client.get(f"{base}/activity", cookies=owner.cookies).json()

    assert [event["event_type"] for event in feed] == [
        "opportunity.created",
        "contact.created",
        "workspace.created",
    ]


def test_clients_cannot_write_to_the_audit_trail(
    client: TestClient, owner: Account, workspace_id: str
) -> None:
    path = f"/v1/workspaces/{workspace_id}/activity"
    forged: dict[str, Any] = {"event_type": "action.approved", "summary": "forged"}

    assert client.post(path, json=forged, cookies=owner.cookies).status_code == 405
    event_id = client.get(path, cookies=owner.cookies).json()[0]["id"]
    assert client.delete(f"{path}/{event_id}", cookies=owner.cookies).status_code == 404
    assert client.patch(f"{path}/{event_id}", json=forged, cookies=owner.cookies).status_code == 404


class TestImmutability:
    @pytest.fixture
    def event(self, db_session: Session, workspace_id: str) -> ActivityEvent:
        recorded = ActivityService(db_session, uuid.UUID(workspace_id)).record(
            actor=Actor.nerva(),
            event_type=ActivityEventType.CONTACT_CREATED,
            entity_type="contact",
            entity_id=uuid.uuid4(),
            summary="Original summary",
        )
        db_session.commit()
        return recorded

    def test_an_event_cannot_be_modified_through_the_orm(
        self, db_session: Session, event: ActivityEvent
    ) -> None:
        event.summary = "Rewritten history"

        with pytest.raises(ImmutableRecordError):
            db_session.flush()

    def test_an_event_cannot_be_deleted_through_the_orm(
        self, db_session: Session, event: ActivityEvent
    ) -> None:
        db_session.delete(event)

        with pytest.raises(ImmutableRecordError):
            db_session.flush()

    def test_the_repository_offers_no_way_to_change_or_remove_events(
        self, db_session: Session, workspace_id: str
    ) -> None:
        repository = ActivityEventRepository(db_session, uuid.UUID(workspace_id))

        for mutator in ("update", "delete", "remove", "save"):
            assert not hasattr(repository, mutator)


def test_the_database_rejects_a_user_event_that_names_no_user(
    db_session: Session, workspace_id: str
) -> None:
    db_session.add(
        ActivityEvent(
            workspace_id=uuid.UUID(workspace_id),
            actor_type=ActorType.USER,
            actor_user_id=None,
            event_type="contact.created",
            entity_type="contact",
            entity_id=uuid.uuid4(),
            summary="Claims to be from a user but names none",
        )
    )

    with pytest.raises(IntegrityError, match="actor_type_matches_user"):
        db_session.flush()
