"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# When the agent runs behind the platform gateway, that gateway calls
# agent.invoke() directly and this module is not in the path.

import os
import secrets
from typing import Any, Dict, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.config_loader import load_runtime_config
from src.graph.graph import Graph
from src.screens import (
    credential_findings,
    privacy_filter_removes_everything,
    screen_structure,
    screen_text,
    validate_context,
)

app = FastAPI(title="Agent")

# Runtime parameters come from config/config.yaml. Constructing the graph
# without them would leave every declared value (max_retry, the routing
# taxonomy) unread while the agent still started and answered — a silent
# degradation to framework defaults.
agent = Graph(config=load_runtime_config())
agent.compile()
agent.provision_secrets(secrets_factory(namespace="svc", agent_name="ServiceRequestClassificationRoutingAgent"))

# Upper bound on the free-text request accepted at the boundary. The domain
# validation in the pipeline applies its own, narrower rules; this one exists so
# an oversized body is rejected before any work starts.
MAX_INPUT_CHARS = 20000


class InvokeRequest(BaseModel):
    """Caller contract for POST /invoke.

    input          free-text service request (ticket, complaint, HR ask …)
    session_id     optional caller-supplied conversation id
    input_context  optional metadata; only `channel` is accepted, and only as a
                   lowercase identifier. Unknown keys are dropped.
    """

    input: str
    session_id: str = ""
    input_context: Optional[Dict[str, Any]] = None


def _reject(detail: str) -> HTTPException:
    """Build the caller-facing refusal.

    400, not 422: the validation layer owns 422 and answers there with a list of
    error objects, so reusing it would make client handling ambiguous.
    """
    return HTTPException(status_code=400, detail=detail)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    # Standalone caller auth: when INVOKE_AUTH_TOKEN is set on the server
    # environment, callers that no upstream middleware vouched for (still
    # ANONYMOUS) must present it as a Bearer token and run at
    # VERIFIED_EXTERNAL. Middleware-established trust is never demoted.
    # This adapter is the entry-point auth boundary — a deployment-level caller
    # credential, not an agent secret, so the per-invocation secret accessor
    # does not apply (no InvocationContext exists before auth).
    expected = os.environ.get("INVOKE_AUTH_TOKEN")
    if expected and trust is TrustLevel.ANONYMOUS:
        supplied = request.headers.get("authorization", "")
        # Compare bytes: compare_digest raises TypeError on non-ASCII str input
        # (headers decode as latin-1), which would 500 instead of the generic 401.
        if not secrets.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
            # Generic body on purpose — do not leak whether the token was absent,
            # malformed, or wrong.
            raise HTTPException(status_code=401, detail="Token is invalid or expired.")
        trust = TrustLevel.VERIFIED_EXTERNAL

    if len(req.input) > MAX_INPUT_CHARS:
        raise _reject(f"input exceeds {MAX_INPUT_CHARS} characters")

    context, context_error = validate_context(req.input_context)
    if context_error:
        raise _reject(context_error)

    # Credential screen, BEFORE invoke(). The first node returns the request
    # text back into its own result, and the platform output gate scans every
    # value of every result — so a credential-shaped string anywhere in the
    # request aborts the run inside the graph with a traceback the caller cannot
    # act on. The request cannot succeed either way; refusing here turns an
    # opaque failure into a specific one. The platform's own detector is used so
    # the refusal set matches the block set exactly.
    if credential_findings(req.input):
        raise _reject(
            "input appears to contain a credential; remove it and resubmit " "(the request text is not stored)"
        )
    for name, value in context.items():
        if credential_findings(value):
            raise _reject(f"input_context.{name} appears to contain a credential")

    # Injection screen at the boundary: the same class the pipeline refuses, so
    # a hostile request is answered with an actionable 400 rather than a generic
    # pipeline error.
    label = screen_text(req.input)
    if label:
        raise _reject(f"input rejected by the request screen ({label})")
    found = screen_structure(context)
    if found:
        raise _reject(f"input_context.{found[0]} rejected by the request screen ({found[1]})")

    # The privacy filter runs on the request before the first node sees it, and
    # it removes a fully title-cased sentence outright — which leaves the
    # pipeline nothing to classify and the caller an error with no cause. Say
    # what happened instead.
    if privacy_filter_removes_everything(req.input):
        raise _reject(
            "the request text was removed in full by the privacy filter — describe the "
            "issue without personal identifiers and resubmit"
        )

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        envelope: Dict[str, Any] = agent.invoke(req.input, ctx=ctx, input_context=context)
        return envelope


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "ServiceRequestClassificationRoutingAgent"}
