"""AgentCore Platform v1.0"""

# SVC-C2-018 — Service Request Classification & Routing Agent
#
# State must be a flat TypedDict — never a Pydantic model. Checkpoints are
# serialised with msgpack, which corrupts model instances silently. Extend
# AgentState with agent-specific fields only, and never put credentials or
# secrets in it.
#
# Every dict/list-valued field is stored as a JSON-serialised Optional[str] for
# the same reason. Use to_json() / from_json() below at every producer and every
# consumer so the contract is identical end to end; typing such a field as a
# bare dict or list reintroduces the serialisation failure.
#
# Security note: routing destinations are resolved from the configured taxonomy
# ONLY (RouteResolveNode) — never derived from the request text. No routing
# target ever flows into this state from the free-text request.

import json
from typing import Any, NotRequired, Optional

from framework.schemas.agent_state import AgentState


def to_json(value: Any) -> Optional[str]:
    """Serialize a value to a JSON string for State storage."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False)


def from_json(value: Optional[str], default: Any = None) -> Any:
    """Deserialize a JSON string from State storage."""
    if value is None:
        return default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


class State(AgentState):
    """Flat TypedDict for SVC-C2-018.

    All shared fields (user_input, status, session_id, node_history, error_log,
    the human-in-the-loop fields, …) are inherited from AgentState.

    formatted_output is NOT re-declared here — it is inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode (pre_process backbone slot)
    # ------------------------------------------------------------------

    # Validated and normalised free-text service request.
    # Produced by PreProcessNode; consumed by the inner InputValidateNode.
    validated_input: NotRequired[Optional[str]]

    # JSON-serialised channel/request metadata.
    # Shape: {"source": str, "channel": str, "length": int}
    enriched_context: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # JSON-serialised normalised service-request payload.
    # Shape: {text: str, channel: str, word_count: int, length: int}
    service_request: NotRequired[Optional[str]]

    # JSON-serialised classification context.
    # Shape: {request_text: str, taxonomy_version: str,
    #         categories: [{id, label, queue, handler}], channel: str}
    classification_context: NotRequired[Optional[str]]

    # JSON-serialised classification result.
    # Shape: {category: str, priority: str, confidence: float,
    #         matched_signals: [str], rationale: str}
    classification_result: NotRequired[Optional[str]]

    # JSON-serialised routing decision.
    # Shape: {category: str, queue: str, handler: str, priority: str,
    #         confidence: float, routed_from_config: bool, fallback: bool}
    # NOTE: queue/handler are resolved from the configured taxonomy ONLY.
    routing_decision: NotRequired[Optional[str]]

    # Final formatted routing response document (plain text).
    # Assembled by the inner OutputFormatNode from routing_decision +
    # classification_result.
    routing_report: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Outer layer — set by PostProcessNode (post_process backbone slot)
    # ------------------------------------------------------------------

    # Primary result surfaced to the caller. Written only by PostProcessNode,
    # and only in lock-step with formatted_output: the envelope builder resolves
    # the caller-visible output as `formatted_output or result` with no status
    # check, so a path that populates one without the other decides what the
    # caller sees by accident.
    result: NotRequired[Optional[str]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: NotRequired[Optional[str]]
    correlation_id: NotRequired[Optional[str]]
    # Set when a run COMPLETES without carrying out the request, because the
    # caller sent a value they can correct. A closed set of codes, never caller
    # content. Nodes downstream of the one that set it do no work and pass it on.
    error_code: Optional[str]
    # node_history is inherited from AgentState
