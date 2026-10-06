"""AgentCore Platform v1.0"""

# SVC-C2-018 — BuildContextNode
# Inner domain node 2: assemble the classification context.
#
# Loads the routing TAXONOMY — the allowed categories and their configured
# queues/handlers — and packages it together with the request text into
# classification_context. This is the ONLY place the taxonomy enters the
# pipeline: GenerateResponseNode classifies AGAINST it and RouteResolveNode
# resolves the queue FROM it, so routing destinations are always configuration-
# sourced and never derived from the request text.
#
# The taxonomy comes from the runtime parameters (config/config.yaml, `routing`
# block), so it is customer-configurable without a code change. When that block
# is absent or malformed, the built-in default in src/config_loader.py applies
# and the audit event records which of the two resolved the request.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict, List

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.config_loader import load_routing_taxonomy
from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)


class BuildContextNode(FunctionNode):
    """Assemble the classification context from the configured taxonomy.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        service_request: str  — JSON-serialised normalised request

    Output state keys (partial dict):
        classification_context: str  — JSON-serialised context
        status:                 str
        error_log:              list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        # Without this the node reports its own precondition failure and the
        # specific, actionable reason is replaced by a vaguer one.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}

        service_request: Dict[str, Any] = from_json(state.get("service_request"), {})

        if not service_request or not service_request.get("text"):
            emit_trace_event("build_context_failed", {"reason": "missing_service_request"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["BuildContextNode: service_request missing in state"],
            }

        taxonomy = load_routing_taxonomy()
        categories: List[Dict[str, Any]] = taxonomy["categories"]

        classification_context: Dict[str, Any] = {
            "request_text": service_request["text"],
            "channel": service_request.get("channel", "unknown"),
            "taxonomy_version": taxonomy["taxonomy_version"],
            "default_category": taxonomy["default_category"],
            "categories": categories,
        }

        logger.info(
            "BuildContextNode: context built taxonomy=%s source=%s categories=%d",
            taxonomy["taxonomy_version"],
            taxonomy["source"],
            len(categories),
        )
        emit_trace_event(
            "build_context_complete",
            {
                "taxonomy_version": taxonomy["taxonomy_version"],
                "taxonomy_source": taxonomy["source"],
                "category_count": len(categories),
                "category_ids": [str(c["id"]) for c in categories],
            },
            state,
        )

        return {
            "classification_context": to_json(classification_context),
            "status": AgentStatus.SUCCESS.value,
        }
