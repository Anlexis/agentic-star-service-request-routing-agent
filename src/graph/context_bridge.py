"""AgentCore Platform v1.0"""

# SVC-C2-018 — context bridge between the outer backbone and the inner workflow.
#
# The subgraph boundary forwards only the request STRING: the inner graph builds
# a fresh state, so nothing the outer nodes wrote — including the validated
# request metadata — is visible to the domain nodes. Without a bridge, the
# channel the caller supplied would be silently replaced by "unknown" at the
# boundary and every downstream reader would see a default it had no way to
# distinguish from a real value.
#
# The bridge is a ContextVar rather than a module global so concurrent requests
# in the same process cannot read each other's metadata: the outer node stashes
# immediately before delegating, and the inner graph seeds its initial state
# from the stash. One value, one request, one task context.

from contextvars import ContextVar, Token
from typing import Any, Dict

_REQUEST_CONTEXT: ContextVar[Dict[str, Any]] = ContextVar("svc_request_context", default={})


def stash_request_context(value: Dict[str, Any]) -> Token[Dict[str, Any]]:
    """Record the request metadata for the inner graph. Returns the reset token."""
    return _REQUEST_CONTEXT.set(dict(value or {}))


def read_request_context() -> Dict[str, Any]:
    """Return the metadata stashed for this request (empty mapping when unset)."""
    return dict(_REQUEST_CONTEXT.get())


def reset_request_context(token: Token[Dict[str, Any]]) -> None:
    """Restore the previous value — used by tests and by nested invocations."""
    _REQUEST_CONTEXT.reset(token)
