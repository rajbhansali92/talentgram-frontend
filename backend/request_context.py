"""Request-scoped correlation ID.

A single ContextVar holding the current request's correlation ID. Set once
per request by RequestIdMiddleware (backend/server.py) before the request is
handled, and readable from anywhere in that request's async call chain —
routers, dependencies, services — without threading it through function
signatures. Later observability phases read this via get_request_id(); this
module only defines the storage and generation, nothing consumes it yet.
"""
from contextvars import ContextVar
from typing import Optional
import uuid

_request_id_ctx_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


def generate_request_id() -> str:
    return str(uuid.uuid4())


def get_request_id() -> Optional[str]:
    """Current request's correlation ID, or None outside a request context."""
    return _request_id_ctx_var.get()


def set_request_id(value: str):
    """Sets the correlation ID for the current context; returns a reset token."""
    return _request_id_ctx_var.set(value)


def reset_request_id(token) -> None:
    _request_id_ctx_var.reset(token)
