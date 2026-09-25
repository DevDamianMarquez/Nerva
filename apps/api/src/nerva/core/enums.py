"""Enumerations shared by models, schemas and services.

Enum columns are stored as VARCHAR with a CHECK constraint (not native PostgreSQL
enums), so adding a value is a simple migration.
"""

from enum import StrEnum


class MemberRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"


class ActorType(StrEnum):
    """Who originated a change. Approvals are always made by a human user."""

    USER = "user"
    NERVA = "nerva"


class ActionStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REJECTED = "rejected"


class ActionType(StrEnum):
    """Kinds of action Nerva can propose. Stored as free text in the database so new
    types do not require a migration; validated against this enum at the API boundary."""

    SEND_EMAIL = "send_email"
    CREATE_TASK = "create_task"
    UPDATE_OPPORTUNITY_STAGE = "update_opportunity_stage"


class OpportunityStage(StrEnum):
    NEW = "new"
    QUALIFIED = "qualified"
    PROPOSAL = "proposal"
    WON = "won"
    LOST = "lost"


class ConversationChannel(StrEnum):
    EMAIL = "email"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MemoryKind(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    NOTE = "note"


class IntegrationProvider(StrEnum):
    """External account providers Nerva Connect can synchronize. One value today;
    the table and API are shaped so a second (e.g. Outlook) needs no redesign."""

    GOOGLE = "google"


class IntegrationStatus(StrEnum):
    CONNECTED = "connected"
    # The refresh token was rejected or revoked; the user must reconnect.
    NEEDS_REAUTH = "needs_reauth"
    # The most recent sync attempt failed for a reason other than authorization.
    ERROR = "error"
    # Disconnected by the user. The row (and its imported conversations/messages)
    # is kept, not deleted; see docs/ARCHITECTURE.md.
    DISCONNECTED = "disconnected"


class ActivityEventType(StrEnum):
    WORKSPACE_CREATED = "workspace.created"
    COMPANY_CREATED = "company.created"
    CONTACT_CREATED = "contact.created"
    OPPORTUNITY_CREATED = "opportunity.created"
    ACTION_PROPOSED = "action.proposed"
    ACTION_APPROVED = "action.approved"
    ACTION_REJECTED = "action.rejected"
    ACTION_EXECUTING = "action.executing"
    ACTION_COMPLETED = "action.completed"
    ACTION_FAILED = "action.failed"
    INTEGRATION_CONNECTED = "integration.connected"
    INTEGRATION_SYNCED = "integration.synced"
    INTEGRATION_DISCONNECTED = "integration.disconnected"
    INTEGRATION_NEEDS_REAUTH = "integration.needs_reauth"
