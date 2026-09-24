"""Companies, contacts and opportunities."""

import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError

from nerva.core.enums import ActivityEventType, OpportunityStage
from nerva.core.errors import ConflictError, NotFoundError
from nerva.models import Company, Contact, Opportunity
from nerva.repositories.workspace_data import (
    CompanyRepository,
    ContactRepository,
    OpportunityRepository,
)
from nerva.schemas.crm import CompanyCreate, ContactCreate, OpportunityCreate
from nerva.services.base import ScopedService


class CompanyService(ScopedService):
    def create(self, data: CompanyCreate) -> Company:
        repo = CompanyRepository(self.session, self.ctx.workspace_id)
        company = repo.add(Company(name=data.name, domain=data.domain))
        self.activity.record(
            actor=self.ctx.actor,
            event_type=ActivityEventType.COMPANY_CREATED,
            entity_type="company",
            entity_id=company.id,
            summary=f"Company “{company.name}” was added.",
        )
        self.session.commit()
        return company

    def get(self, company_id: uuid.UUID) -> Company:
        company = CompanyRepository(self.session, self.ctx.workspace_id).get(company_id)
        if company is None:
            raise NotFoundError("Company not found.")
        return company

    def list(self, *, limit: int, offset: int) -> Sequence[Company]:
        return CompanyRepository(self.session, self.ctx.workspace_id).list(
            limit=limit, offset=offset
        )


class ContactService(ScopedService):
    def create(self, data: ContactCreate) -> Contact:
        repo = ContactRepository(self.session, self.ctx.workspace_id)
        self._require_company(data.company_id)
        if repo.get_by_email(data.email) is not None:
            raise ConflictError("A contact with this email already exists in this workspace.")
        try:
            contact = repo.add(
                Contact(
                    email=data.email,
                    name=data.name,
                    title=data.title,
                    company_id=data.company_id,
                )
            )
            self.activity.record(
                actor=self.ctx.actor,
                event_type=ActivityEventType.CONTACT_CREATED,
                entity_type="contact",
                entity_id=contact.id,
                summary=f"Contact {contact.name or contact.email} was added.",
            )
            self.session.commit()
        except IntegrityError as exc:  # concurrent create with the same email
            self.session.rollback()
            raise ConflictError(
                "A contact with this email already exists in this workspace."
            ) from exc
        return contact

    def get(self, contact_id: uuid.UUID) -> Contact:
        contact = ContactRepository(self.session, self.ctx.workspace_id).get(contact_id)
        if contact is None:
            raise NotFoundError("Contact not found.")
        return contact

    def list(self, *, limit: int, offset: int) -> Sequence[Contact]:
        return ContactRepository(self.session, self.ctx.workspace_id).list(
            limit=limit, offset=offset
        )


class OpportunityService(ScopedService):
    def create(self, data: OpportunityCreate) -> Opportunity:
        self._require_contact(data.contact_id)
        self._require_company(data.company_id)
        self._require_conversation(data.conversation_id)
        opportunity = OpportunityRepository(self.session, self.ctx.workspace_id).add(
            Opportunity(
                title=data.title,
                summary=data.summary,
                stage=data.stage,
                estimated_value_cents=data.estimated_value_cents,
                currency=data.currency,
                contact_id=data.contact_id,
                company_id=data.company_id,
                conversation_id=data.conversation_id,
                created_by_type=self.ctx.actor.type,
                created_by_user_id=self.ctx.actor.user_id,
            )
        )
        self.activity.record(
            actor=self.ctx.actor,
            event_type=ActivityEventType.OPPORTUNITY_CREATED,
            entity_type="opportunity",
            entity_id=opportunity.id,
            summary=f"Opportunity “{opportunity.title}” was created.",
            data={"stage": opportunity.stage.value},
        )
        self.session.commit()
        return opportunity

    def get(self, opportunity_id: uuid.UUID) -> Opportunity:
        opportunity = OpportunityRepository(self.session, self.ctx.workspace_id).get(opportunity_id)
        if opportunity is None:
            raise NotFoundError("Opportunity not found.")
        return opportunity

    def list(
        self, *, stage: OpportunityStage | None, limit: int, offset: int
    ) -> Sequence[Opportunity]:
        return OpportunityRepository(self.session, self.ctx.workspace_id).list_filtered(
            stage=stage, limit=limit, offset=offset
        )
