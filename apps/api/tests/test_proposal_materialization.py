"""`ProposalMaterializationService`: M7.1c's deterministic, atomic gate
between M7.1b's `AllowedProposal` and a persisted `Action(PROPOSED)` —
against a real database, including true multi-connection concurrency.
Mirrors `test_memory_formation.py`'s structure and reasoning for the same
kind of problem (an automated, idempotent write that must survive a real
race, not just a single-process check-then-insert).

No AI provider, no prompt, no orchestration, no Gmail, no execution/
approval of any kind — none of that exists yet (M7.1d+).

M8.1 added a composite tenant FK on `Action.conversation_id` (see
`nerva.models.actions.Action`'s own docstring), so `ProposalProvenance.
conversation_id` passed to `materialize()` must now name a real `Conversation`
row in the same workspace — `make_service` creates one alongside the service
for exactly that reason; a bare, unattached UUID (as this file used before
M8.1) would now fail with a `ForeignKeyViolation`."""

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionStatus, ActionType, ActivityEventType, ActorType, MemberRole
from nerva.models import Action, Conversation, User, Workspace
from nerva.nerva_actions.payloads import SendEmailPayload
from nerva.nerva_core.proposal_fingerprint import ProposalProvenance
from nerva.nerva_core.proposal_policy import AllowedProposal
from nerva.repositories.workspace_data import ActionRepository, ActivityEventRepository
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from nerva.services.proposal_materialization import (
    DuplicateProposal,
    MaterializedProposal,
    ProposalMaterializationService,
)

ANALYSIS_ID = uuid.uuid4()


def _workspace(session: Session, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    session.add(workspace)
    session.flush()
    return workspace


def _user(session: Session, name: str = "Alex") -> User:
    user = User(
        email=f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com", name=name, password_hash="x"
    )
    session.add(user)
    session.flush()
    return user


def _ctx(session: Session, workspace: Workspace) -> WorkspaceContext:
    user = _user(session)
    return WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)


def _conversation(session: Session, workspace: Workspace) -> Conversation:
    """A real `Conversation` row in `workspace` — the only thing M8.1's
    composite FK on `Action.conversation_id` requires `ProposalProvenance.
    conversation_id` to name. `Conversation.id` is a plain, globally-unique
    primary key (not composite), so two different workspaces can never share
    one — see `test_the_same_proposal_fingerprint_in_two_different_
    workspaces_does_not_conflict` for why that matters."""
    conversation = Conversation(workspace_id=workspace.id, subject="Pricing question")
    session.add(conversation)
    session.flush()
    return conversation


def _payload(**overrides: object) -> SendEmailPayload:
    defaults: dict[str, object] = {
        "recipients": ["priya@halcyon.example"],
        "subject": "Pricing for 3 seats",
        "body": "Hi Priya, here is the pricing you asked about.",
    }
    defaults.update(overrides)
    return SendEmailPayload.model_validate(defaults)


def _allowed(**overrides: object) -> AllowedProposal:
    defaults: dict[str, object] = {
        "action_type": ActionType.SEND_EMAIL,
        "summary": "Reply with pricing for 3 seats.",
        "validated_payload": _payload(),
        "reason": "The prospect asked for pricing.",
        "confidence": 0.8,
        "grounded_evidence": ("message_1",),
    }
    defaults.update(overrides)
    return AllowedProposal(**defaults)  # type: ignore[arg-type]


def _provenance(*, conversation_id: uuid.UUID, **overrides: object) -> ProposalProvenance:
    defaults: dict[str, object] = {
        "conversation_id": conversation_id,
        "analysis_id": ANALYSIS_ID,
        "source_fingerprint": "fp-reasoning-state-1",
        "reasoning_version": "test-reasoning-v0",
        "proposal_policy_version": "v1",
    }
    defaults.update(overrides)
    return ProposalProvenance(**defaults)  # type: ignore[arg-type]


@dataclass
class ServiceFixture:
    service: ProposalMaterializationService
    conversation_id: uuid.UUID

    def provenance(self, **overrides: object) -> ProposalProvenance:
        return _provenance(conversation_id=self.conversation_id, **overrides)


MakeService = Callable[[Workspace], ServiceFixture]


@pytest.fixture
def make_service(db_session: Session) -> MakeService:
    def _make(workspace: Workspace) -> ServiceFixture:
        ctx = _ctx(db_session, workspace)
        conversation = _conversation(db_session, workspace)
        return ServiceFixture(
            service=ProposalMaterializationService(db_session, ctx),
            conversation_id=conversation.id,
        )

    return _make


# ---------------------------------------------------------------------------
# A. Creation (1-4)
# ---------------------------------------------------------------------------


def test_a_valid_allowed_proposal_creates_a_proposed_action(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.status == ActionStatus.PROPOSED
    assert outcome.action.type == "send_email"


def test_the_action_is_attributed_to_nerva(db_session: Session, make_service: MakeService) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.created_by_type == ActorType.NERVA


def test_created_by_user_id_is_none(db_session: Session, make_service: MakeService) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.created_by_user_id is None


def test_the_proposal_fingerprint_is_persisted(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.proposal_fingerprint is not None
    assert len(outcome.action.proposal_fingerprint) == 64


def test_the_action_carries_the_provenance_conversation_id(
    db_session: Session, make_service: MakeService
) -> None:
    """M8.1: the only authoritative source for an automatic Action's
    `conversation_id` is `ProposalProvenance.conversation_id`."""
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.conversation_id == fixture.conversation_id


# ---------------------------------------------------------------------------
# B. Deduplication (5-8, 17)
# ---------------------------------------------------------------------------


def test_the_same_allowed_proposal_twice_is_a_duplicate_the_second_time(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    allowed, provenance = _allowed(), fixture.provenance()

    first = fixture.service.materialize(allowed, provenance=provenance)
    second = fixture.service.materialize(allowed, provenance=provenance)

    assert isinstance(first, MaterializedProposal)
    assert isinstance(second, DuplicateProposal)
    assert second.existing_action.id == first.action.id


def test_only_one_action_row_exists_after_a_duplicate_attempt(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    allowed, provenance = _allowed(), fixture.provenance()

    fixture.service.materialize(allowed, provenance=provenance)
    fixture.service.materialize(allowed, provenance=provenance)

    all_actions = ActionRepository(db_session, workspace.id).list_filtered(
        status=None, limit=100, offset=0
    )
    assert len(all_actions) == 1


def test_a_duplicate_attempt_creates_no_second_action_proposed_event(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    allowed, provenance = _allowed(), fixture.provenance()

    fixture.service.materialize(allowed, provenance=provenance)
    fixture.service.materialize(allowed, provenance=provenance)

    events = ActivityEventRepository(db_session, workspace.id).list_filtered(
        entity_type=None, entity_id=None, limit=100, offset=0
    )
    proposed = [e for e in events if e.event_type == ActivityEventType.ACTION_PROPOSED.value]
    assert len(proposed) == 1


def test_the_same_proposal_fingerprint_in_two_different_workspaces_does_not_conflict(
    db_session: Session,
) -> None:
    """The partial unique index on `(workspace_id, proposal_fingerprint)` is
    scoped per workspace. Proven directly via `ActionRepository`/raw ORM
    insertion — the same shortcut `test_the_database_itself_rejects_a_
    duplicate_fingerprint_independent_of_any_precheck` already takes —
    rather than through two `materialize()` calls: M8.1's composite FK on
    `Action.conversation_id` means two *different* workspaces can never
    share one real `Conversation.id` (it is a plain, globally-unique primary
    key), so two genuinely independent `materialize()` calls can no longer
    produce byte-identical fingerprints across workspaces in the first
    place — `conversation_id` participates in the hash
    (`nerva_core.proposal_fingerprint`). What this test actually guarantees —
    that the same fingerprint *value* does not conflict across workspaces —
    is still exactly true, and is exercised here the same way the
    same-workspace *conflict* case already is."""
    workspace_a = _workspace(db_session, "Acme")
    workspace_b = _workspace(db_session, "Globex")
    db_session.add_all(
        [
            Action(
                workspace_id=workspace_a.id,
                type="send_email",
                summary="first",
                proposal_fingerprint="b" * 64,
                created_by_type=ActorType.NERVA,
            ),
            Action(
                workspace_id=workspace_b.id,
                type="send_email",
                summary="second",
                proposal_fingerprint="b" * 64,
                created_by_type=ActorType.NERVA,
            ),
        ]
    )

    db_session.flush()  # must not raise

    all_actions = list(ActionRepository(db_session, workspace_a.id).list_filtered(
        status=None, limit=100, offset=0
    )) + list(ActionRepository(db_session, workspace_b.id).list_filtered(
        status=None, limit=100, offset=0
    ))
    assert len({a.id for a in all_actions}) == 2


def test_the_database_itself_rejects_a_duplicate_fingerprint_independent_of_any_precheck(
    db_session: Session,
) -> None:
    """Mirrors `test_workspace_isolation.py::TestDatabaseEnforcesTenantBoundaries`:
    proves the partial unique index itself is what blocks a duplicate,
    independent of any application-level check — by inserting directly via
    the ORM, bypassing `ProposalMaterializationService`/`ActionService`
    entirely."""
    workspace = _workspace(db_session)
    db_session.add(
        Action(
            workspace_id=workspace.id,
            type="send_email",
            summary="first",
            proposal_fingerprint="a" * 64,
            created_by_type=ActorType.NERVA,
        )
    )
    db_session.flush()

    db_session.add(
        Action(
            workspace_id=workspace.id,
            type="send_email",
            summary="second",
            proposal_fingerprint="a" * 64,
            created_by_type=ActorType.NERVA,
        )
    )
    with pytest.raises(
        IntegrityError, match="uq_actions_workspace_id_proposal_fingerprint_partial"
    ):
        db_session.flush()


# ---------------------------------------------------------------------------
# C. Human proposals keep working (9)
# ---------------------------------------------------------------------------


def test_a_human_proposed_action_still_works_with_a_null_fingerprint(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    user = _user(db_session)
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
    service = ActionService(db_session, ctx)

    action = service.propose(
        ActionCreate(type=ActionType.SEND_EMAIL, summary="Human-proposed", payload={})
    )

    assert action.proposal_fingerprint is None
    assert action.conversation_id is None
    assert action.created_by_type == ActorType.USER


def test_two_human_proposals_both_with_null_fingerprint_do_not_conflict(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    user = _user(db_session)
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
    service = ActionService(db_session, ctx)

    first = service.propose(ActionCreate(type=ActionType.SEND_EMAIL, summary="First", payload={}))
    second = service.propose(ActionCreate(type=ActionType.SEND_EMAIL, summary="Second", payload={}))

    assert first.proposal_fingerprint is None
    assert second.proposal_fingerprint is None
    assert first.id != second.id


# ---------------------------------------------------------------------------
# D. Provenance in the single ACTION_PROPOSED event (10-13)
# ---------------------------------------------------------------------------


def test_provenance_appears_on_the_single_action_proposed_event(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    allowed = _allowed()
    provenance = fixture.provenance(provider_prompt_version="v1")

    outcome = fixture.service.materialize(allowed, provenance=provenance)
    assert isinstance(outcome, MaterializedProposal)

    events = ActivityEventRepository(db_session, workspace.id).list_filtered(
        entity_type="action", entity_id=outcome.action.id, limit=10, offset=0
    )
    proposed = [e for e in events if e.event_type == ActivityEventType.ACTION_PROPOSED.value]
    assert len(proposed) == 1
    data = proposed[0].data
    assert data["conversation_id"] == str(provenance.conversation_id)
    assert data["analysis_id"] == str(provenance.analysis_id)
    assert data["source_fingerprint"] == provenance.source_fingerprint
    assert data["reasoning_version"] == provenance.reasoning_version
    assert data["proposal_policy_version"] == provenance.proposal_policy_version
    assert data["provider_prompt_version"] == "v1"
    assert data["proposal_fingerprint"] == outcome.action.proposal_fingerprint


def test_a_none_provider_prompt_version_is_recorded_as_null(
    db_session: Session, make_service: MakeService
) -> None:
    """M7.1e: a provider with no real prompt (the fake, or an unconfigured
    client) records `None` honestly rather than a fabricated version."""
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    provenance = fixture.provenance(provider_prompt_version=None)

    outcome = fixture.service.materialize(_allowed(), provenance=provenance)
    assert isinstance(outcome, MaterializedProposal)

    events = ActivityEventRepository(db_session, workspace.id).list_filtered(
        entity_type="action", entity_id=outcome.action.id, limit=10, offset=0
    )
    proposed = [e for e in events if e.event_type == ActivityEventType.ACTION_PROPOSED.value]
    assert proposed[0].data["provider_prompt_version"] is None


def test_confidence_and_reason_are_audited_on_the_event(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    allowed = _allowed(confidence=0.42, reason="Because the prospect explicitly asked.")

    outcome = fixture.service.materialize(allowed, provenance=fixture.provenance())
    assert isinstance(outcome, MaterializedProposal)

    events = ActivityEventRepository(db_session, workspace.id).list_filtered(
        entity_type="action", entity_id=outcome.action.id, limit=10, offset=0
    )
    data = events[0].data
    assert data["confidence"] == 0.42
    assert data["reason"] == "Because the prospect explicitly asked."


def test_grounded_evidence_is_audited_on_the_event(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)
    allowed = _allowed(grounded_evidence=("message_1", "analysis"))

    outcome = fixture.service.materialize(allowed, provenance=fixture.provenance())
    assert isinstance(outcome, MaterializedProposal)

    events = ActivityEventRepository(db_session, workspace.id).list_filtered(
        entity_type="action", entity_id=outcome.action.id, limit=10, offset=0
    )
    assert events[0].data["grounded_evidence"] == ["message_1", "analysis"]


def test_no_raw_memory_or_conversation_content_is_audited() -> None:
    """Architectural property, not a per-call check: `_provenance_event_data`
    only ever reads fields already present on `ProposalProvenance`/
    `AllowedProposal` — neither carries a raw memory statement or a raw
    conversation/message body (see `nerva_ai.agent_client`'s own "no
    internal ids/raw content" discipline, inherited here) — so there is
    nothing for this function to leak even by accident."""
    from dataclasses import fields

    provenance_fields = {f.name for f in fields(ProposalProvenance)}
    allowed_fields = {f.name for f in fields(AllowedProposal)}
    forbidden = {"body_text", "message_body", "memory_statement", "raw_content"}

    assert not (provenance_fields & forbidden)
    assert not (allowed_fields & forbidden)


# ---------------------------------------------------------------------------
# E. Security: never approve, never execute (14/15)
# ---------------------------------------------------------------------------


def test_materialize_never_approves_the_action(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.status == ActionStatus.PROPOSED
    assert outcome.action.approved_by_user_id is None
    assert outcome.action.approved_at is None


def test_materialize_never_executes_the_action(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    fixture = make_service(workspace)

    outcome = fixture.service.materialize(_allowed(), provenance=fixture.provenance())

    assert isinstance(outcome, MaterializedProposal)
    assert outcome.action.started_at is None
    assert outcome.action.finished_at is None
    assert outcome.action.result is None


def test_proposal_materialization_service_has_no_approve_or_execute_method() -> None:
    assert not hasattr(ProposalMaterializationService, "approve")
    assert not hasattr(ProposalMaterializationService, "execute")
    assert not hasattr(ProposalMaterializationService, "start_execution")


# ---------------------------------------------------------------------------
# F. Real, multi-connection concurrency (18)
# ---------------------------------------------------------------------------


def _real_session(engine: Engine) -> Session:
    return Session(bind=engine, expire_on_commit=False)


def _cleanup(session: Session, workspace: Workspace, user: User) -> None:
    session.delete(session.get(Workspace, workspace.id))  # cascades to every owned row
    session.delete(session.get(User, user.id))
    session.commit()
    session.close()


def _run_and_join(threads: list[threading.Thread], *, timeout: float = 30) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    for thread in threads:
        assert not thread.is_alive(), "a thread did not finish in time — likely stuck on a lock"


def test_two_concurrent_identical_proposals_never_both_succeed(engine: Engine) -> None:
    """The real property this block exists to guarantee: two independent
    connections racing to materialize the exact same proposal identity must
    leave exactly one `Action` row, regardless of which one PostgreSQL lets
    win — proven with true threads and true connections, not two calls on
    one session (`db_session`'s savepoint-only commits would not exercise a
    real cross-connection race at all — see `test_memory_formation.py`'s
    own module docstring for the identical reasoning)."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Concurrency")
    user = _user(setup)
    conversation = _conversation(setup, workspace)
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
    allowed = _allowed()
    provenance = _provenance(conversation_id=conversation.id)

    try:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def _call(key: str) -> None:
            session = _real_session(engine)
            try:
                barrier.wait(timeout=10)
                service = ProposalMaterializationService(session, ctx)
                outcome = service.materialize(allowed, provenance=provenance)
                if isinstance(outcome, MaterializedProposal):
                    results[key] = ("created", outcome.action.id)
                else:
                    results[key] = ("duplicate", outcome.existing_action.id)
            except BaseException as exc:  # surfaced on the main thread below
                results[key] = exc
            finally:
                session.close()

        threads = [threading.Thread(target=_call, args=(key,)) for key in ("a", "b")]
        _run_and_join(threads)

        outcomes: dict[str, tuple[str, uuid.UUID]] = {}
        for key in ("a", "b"):
            result = results[key]
            if isinstance(result, BaseException):
                raise result
            assert isinstance(result, tuple)
            outcomes[key] = result

        kind_a, id_a = outcomes["a"]
        kind_b, id_b = outcomes["b"]
        # Exactly one of the two created it; the other saw it as a duplicate
        # of the exact same row — never two different Action ids, never two
        # failures, never two creations.
        assert {kind_a, kind_b} == {"created", "duplicate"}
        assert id_a == id_b

        verify = _real_session(engine)
        all_actions = ActionRepository(verify, workspace.id).list_filtered(
            status=None, limit=100, offset=0
        )
        assert len(all_actions) == 1
        verify.close()
    finally:
        _cleanup(setup, workspace, user)
