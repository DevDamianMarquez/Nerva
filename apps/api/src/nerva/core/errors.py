"""Domain errors and their mapping to a single, consistent HTTP error shape.

Every error response has the form::

    {"error": {"code": "not_found", "message": "Contact not found", "details": null}}
"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class NervaError(Exception):
    """Base class for errors that are safe to show to API clients."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class AuthenticationError(NervaError):
    status_code = 401
    code = "unauthenticated"


class AuthorizationError(NervaError):
    """The caller is authenticated and a workspace member, but lacks permission for
    this specific operation (e.g. a non-owner managing an integration).

    Unlike `NotFoundError`, this is used only where the caller already knows the
    resource exists — it must never be used in place of `NotFoundError` for
    cross-tenant access, which stays a 404 so tenants cannot be enumerated.
    """

    status_code = 403
    code = "forbidden"


class NotFoundError(NervaError):
    """Also used for resources in a workspace the caller does not belong to,
    so that the existence of other tenants' data is never revealed."""

    status_code = 404
    code = "not_found"


class ConflictError(NervaError):
    status_code = 409
    code = "conflict"


class InvalidStateTransitionError(ConflictError):
    code = "invalid_state_transition"


class InvalidReferenceError(NervaError):
    """A request body references an entity that does not exist in the caller's workspace."""

    status_code = 422
    code = "invalid_reference"


class ServiceUnavailableError(NervaError):
    """A required external dependency is not configured or could not be reached
    (e.g. Google OAuth credentials are not set). Distinct from a 500: the request
    itself was fine, but this deployment cannot fulfill it right now."""

    status_code = 503
    code = "service_unavailable"


class ImmutableRecordError(RuntimeError):
    """Raised when code attempts to modify or delete an append-only record."""


def _error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NervaError)
    async def _nerva_error(_: Request, exc: NervaError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(
                _error_body("validation_error", "The request is not valid.", details)
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(code, str(exc.detail)),
            headers=getattr(exc, "headers", None),
        )
