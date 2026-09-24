"""Schema base classes and shared response models."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class RequestModel(BaseModel):
    """Base for request bodies.

    `extra="forbid"` means unknown fields (notably a client-supplied `workspace_id`)
    are rejected with a 422 instead of being silently ignored.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    details: Any = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


# Documented on every workspace-scoped route in the OpenAPI schema.
WORKSPACE_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Missing or unknown user."},
    404: {"model": ErrorResponse, "description": "Not found, or not a member of the workspace."},
    422: {"model": ErrorResponse, "description": "Validation error."},
}
