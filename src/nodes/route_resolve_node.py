"""AgentCore Platform v1.0"""

# SVC-C2-018 — RouteResolveNode
# Inner domain node 4 (domain-fit middle): resolve the classified category into
# a concrete routing target (queue + handler).
#
# SECURITY — routing-injection defence:
#   Routing destinations are resolved ONLY from the configured taxonomy carried
#   in classification_context (populated by BuildContextNode). The category id
#   proposed upstream is a LOOKUP KEY, never a queue name. If the proposed
#   category is not present in the taxonomy — an injected or unknown value —
#   routing falls back to the taxonomy's default category. A queue name is NEVER
#   read from the free-text request or from the classification rationale.
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

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)


class RouteResolveNode(FunctionNode):
    """Resolve the classified category to a config-defined routing target.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        classification_result:  str  — JSON classification from GenerateResponseNode
        classification_context: str  — JSON context (carries the configured taxonomy)

    Output state keys (partial dict):
        routing_decision: str  — JSON routing decision
        status:           str
        error_log:        list[str]  (only on ERROR)
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

        classification: Dict[str, Any] = from_json(state.get("classification_result"), {})
        context: Dict[str, Any] = from_json(state.get("classification_context"), {})

        if not classification or not classification.get("category"):
            emit_trace_event(
                "route_resolve_failed",
                {"reason": "missing_classification_result"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["RouteResolveNode: classification_result missing in state"],
            }

        # Build the configuration-sourced routing table
        # {category_id: {queue, handler, ...}}. This table is the ONLY source of
        # routing destinations.
        categories: List[Dict[str, Any]] = context.get("categories", []) or []
        routing_table: Dict[str, Dict[str, Any]] = {c["id"]: c for c in categories if c.get("id")}
        default_id = context.get("default_category")

        proposed_category = str(classification.get("category", ""))

        # Resolve strictly against the configured taxonomy.
        if proposed_category in routing_table:
            target = routing_table[proposed_category]
            resolved_category = proposed_category
            fallback = False
        elif default_id and default_id in routing_table:
            # Unknown or injected category → safe default queue, never request-derived.
            target = routing_table[default_id]
            resolved_category = default_id
            fallback = True
            logger.warning(
                "RouteResolveNode: category '%s' not in taxonomy — routing to default '%s'",
                proposed_category,
                default_id,
            )
        else:
            emit_trace_event(
                "route_resolve_failed",
                {"reason": "empty_routing_taxonomy", "proposed_category": proposed_category},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["RouteResolveNode: routing taxonomy missing/empty in classification_context"],
            }

        routing_decision: Dict[str, Any] = {
            "category": resolved_category,
            "queue": target["queue"],
            "handler": target.get("handler", ""),
            "priority": classification.get("priority", target.get("default_priority", "low")),
            "confidence": classification.get("confidence", 0.0),
            "routed_from_config": True,
            "fallback": fallback,
        }

        logger.info(
            "RouteResolveNode: category=%s queue=%s fallback=%s",
            resolved_category,
            target["queue"],
            fallback,
        )
        emit_trace_event(
            "route_resolve_complete",
            {
                "category": resolved_category,
                "queue": target["queue"],
                "priority": routing_decision["priority"],
                "fallback": fallback,
            },
            state,
        )

        return {
            "routing_decision": to_json(routing_decision),
            "status": AgentStatus.SUCCESS.value,
        }
