"""Assembles the versioned API."""

from fastapi import APIRouter

from nerva.routes import (
    actions,
    activity,
    analysis,
    auth,
    companies,
    contacts,
    conversations,
    integrations,
    opportunities,
    signals,
    workspaces,
)

api_router = APIRouter(prefix="/v1")
api_router.include_router(auth.router)
api_router.include_router(workspaces.router)
api_router.include_router(companies.router)
api_router.include_router(contacts.router)
api_router.include_router(opportunities.router)
api_router.include_router(actions.router)
api_router.include_router(activity.router)
api_router.include_router(conversations.router)
api_router.include_router(integrations.router)
api_router.include_router(integrations.callback_router)
api_router.include_router(analysis.router)
api_router.include_router(signals.router)
