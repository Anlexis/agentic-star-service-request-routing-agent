"""AgentCore Platform v1.0"""

# SVC-C2-018 — OutputFormatNode (inner domain node 5, last in the workflow)
# Assembles the final structured routing response from routing_decision and
# classification_result. This is the last inner node — it produces the
# routing_report string that the outer PostProcessNode gates.
#
# Rendering rules:
#   - every rendered field is drawn from the configured taxonomy or from the
#     deterministic classifier's own vocabulary; no request text is echoed into
#     the response, so a caller cannot write into its own routing document;
#   - the confidence is parsed through a finite, bounded parser before it is
#     formatted as a percentage. NaN and the infinities survive float() and then
#     compare False against every bound, so a plain range check on them would
#     fail open and render "nan%" into the document.
#
# Inner node — ANONYMOUS trust.
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.schemas.state import from_json
from src.screens import INERT_IDENTIFIER_RE, finite_in_range

logger = logging.getLogger(__name__)

_SEPARATOR = "=" * 60

# Priority labels the document may render. Anything else is reported as
# "unspecified" rather than passed through — the label is part of the routing
# document, and an unrecognised one would be text of unknown provenance.
_KNOWN_PRIORITIES = ("low", "medium", "high", "critical")


def _render_confidence(value: Any) -> str:
    """Format the classifier confidence, or "n/a" when it is not a usable number."""
    parsed = finite_in_range(value, 0.0, 1.0)
    return "n/a" if parsed is None else f"{parsed:.0%}"


def _render_priority(value: Any) -> str:
    """Return an uppercase priority label from the known set."""
    label = str(value).strip().lower()
    return label.upper() if label in _KNOWN_PRIORITIES else "UNSPECIFIED"


def _render_channel(value: Any) -> str:
    """Render the origin channel, or "unknown" when it is not an inert identifier.

    This is the one caller-supplied value that reaches the document. It is
    already locked to a lowercase identifier at the entry point; re-checking it
    here means the document cannot carry caller-written text even if it is
    assembled from state some other component populated.
    """
    if not isinstance(value, str) or not INERT_IDENTIFIER_RE.match(value):
        return "unknown"
    return value


def _assemble_report(routing: Dict[str, Any], classification: Dict[str, Any], channel: Any = None) -> str:
    """Render the routing decision as a formatted response document."""
    signals = classification.get("matched_signals", []) or []
    lines = [
        _SEPARATOR,
        "SERVICE REQUEST ROUTING DECISION",
        _SEPARATOR,
        f"Channel:     {_render_channel(channel)}",
        f"Category:    {routing.get('category', 'unknown')}",
        f"Route Queue: {routing.get('queue', 'unassigned')}",
        f"Handler:     {routing.get('handler', 'unassigned')}",
        f"Priority:    {_render_priority(routing.get('priority'))}",
        f"Confidence:  {_render_confidence(routing.get('confidence'))}",
        f"Fallback:    {'YES (default triage)' if routing.get('fallback') else 'NO'}",
        "",
        "Classification Rationale:",
        f"  {classification.get('rationale', 'N/A')}",
        f"  Matched signals: {', '.join(signals) if signals else '(none)'}",
        "",
        "Routing source: configured taxonomy (routed_from_config=" f"{routing.get('routed_from_config', True)})",
        _SEPARATOR,
    ]
    return "\n".join(lines)


class OutputFormatNode(FunctionNode):
    """Assemble the final routing response document (inner domain node).

    Reads routing_decision and classification_result from State, renders the
    structured routing response text, and writes it to routing_report for the
    outer PostProcessNode.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        routing_decision:      str  — JSON routing decision
        classification_result: str  — JSON classification
        service_request:       str  — JSON normalised request (carries the channel)

    Output state keys (partial dict):
        routing_report: str
        status:         str
        error_log:      list[str]  (only on ERROR)
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

        routing: Dict[str, Any] = from_json(state.get("routing_decision"), {})
        classification: Dict[str, Any] = from_json(state.get("classification_result"), {})

        if not routing:
            logger.error("OutputFormatNode: routing_decision missing in state")
            emit_trace_event("output_format_failed", {"reason": "missing_routing_decision"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["OutputFormatNode: routing_decision missing in state"],
            }

        service_request: Dict[str, Any] = from_json(state.get("service_request"), {}) or {}
        report = _assemble_report(routing, classification, service_request.get("channel"))

        logger.info(
            "OutputFormatNode: report assembled category=%s queue=%s chars=%d",
            routing.get("category", "unknown"),
            routing.get("queue", "unassigned"),
            len(report),
        )
        emit_trace_event(
            "output_format_complete",
            {
                "category": routing.get("category", "unknown"),
                "queue": routing.get("queue", "unassigned"),
                "report_length": len(report),
            },
            state,
        )

        return {
            "routing_report": report,
            "status": AgentStatus.SUCCESS.value,
        }
