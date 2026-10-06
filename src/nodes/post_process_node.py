"""AgentCore Platform v1.0"""

# SVC-C2-018 — PostProcessNode
# Outer backbone post_process slot: the output gate. It enforces two independent
# invariants on the assembled routing response before the caller sees it, and it
# is the only node that writes formatted_output / result.
#
#   1. No credential-shaped string may appear in the response. The scan
#      delegates to the platform's own detector, so the template can never pass
#      a value the platform would go on to refuse.
#   2. Every routing destination in the response must be declared in the
#      configured taxonomy. That is this agent's stated guarantee — destinations
#      come from configuration, never from the request text — and a guarantee
#      the output boundary does not check is a guarantee only the happy path
#      keeps.
#
# On a violation the node returns ERROR **and clears every output-bearing
# field**. Clearing is the part that matters: the envelope builder resolves the
# caller-visible output as `formatted_output or result`, with no status check,
# so an error that leaves result populated ships the un-gated answer inside the
# error envelope. For the same reason the withheld-notice is non-empty — an
# empty string is falsy and re-opens that fallback.
#
# The gate is a module-level function called from execute(), not a method on the
# node class: the node's own security-gate methods are final and reserved by the
# framework.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
import re
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

from src.config_loader import load_routing_taxonomy
from src.schemas.state import from_json
from src.screens import credential_findings

logger = logging.getLogger(__name__)

# One pattern the platform detector does not carry: an inline assignment of a
# named secret ("password: …", "api_key = …"). It is additive — the platform's
# own set is consulted first and in full, so this can only widen the block set,
# never narrow it.
_ASSIGNMENT_PATTERN = re.compile(
    r"(?:password|passwd|secret|api_key|token|access_key|private_key)\s*[:=]\s*\S{8,}",
    re.IGNORECASE,
)

# Closed-set violation labels. The caller and the audit trail only ever see one
# of these — never the matched text, which is the value the gate exists to
# withhold.
_VIOLATION_CREDENTIAL = "credential_pattern"
_VIOLATION_ASSIGNMENT = "credential_assignment"
_VIOLATION_UNDECLARED_ROUTE = "undeclared_routing_destination"

# Non-empty on purpose: a falsy replacement re-opens the `formatted_output or
# result` fallback and ships exactly what the gate withheld.
_WITHHELD_NOTICE = (
    "[Service Request Routing] The routing response was withheld by the output gate. "
    "No routing decision is included in this reply. Contact the service-desk "
    "administrator with the correlation id from this response."
)

# Every output-bearing field this agent can carry. The gate blanks all of them
# on a violation so a checkpoint or a downstream reader cannot pick up the
# withheld content from state either.
_OUTPUT_BEARING_FIELDS: Tuple[str, ...] = (
    "result",
    "routing_report",
    "routing_decision",
    "classification_result",
)


def _security_gate_output(content: str) -> Optional[str]:
    """Scan the assembled response for credential shapes.

    Returns a closed-set violation label, or None when the response is clean.
    """
    if credential_findings(content):
        return _VIOLATION_CREDENTIAL
    if _ASSIGNMENT_PATTERN.search(content):
        return _VIOLATION_ASSIGNMENT
    return None


def _routing_destinations_declared(routing: Dict[str, Any]) -> bool:
    """True when the resolved category and queue are both declared in the taxonomy.

    An empty routing decision counts as declared: there is no destination to
    check, and the empty-response path is handled separately.
    """
    if not routing:
        return True
    taxonomy = load_routing_taxonomy()
    declared: List[Dict[str, Any]] = taxonomy["categories"]
    by_id = {str(entry["id"]): entry for entry in declared}
    entry = by_id.get(str(routing.get("category", "")))
    if entry is None:
        return False
    return str(routing.get("queue", "")) == str(entry.get("queue", ""))


def _cleared_output_state() -> Dict[str, str]:
    """Blank every output-bearing field.

    Kept as one function so the cleared set and the inventory guard in the tests
    cannot drift apart: a new output-bearing field has to be added here to be
    written at all.
    """
    return {field: "" for field in _OUTPUT_BEARING_FIELDS}


# Reason code -> the sentence the caller reads. A code with no entry falls back
# to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES: Dict[str, str] = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """Apply the output gate and expose the final routing response.

    Outer backbone post_process slot. Declared ANONYMOUS — caller trust was
    already enforced at PreProcessNode (VERIFIED_EXTERNAL).

    Input state keys:
        routing_report:   str  — formatted routing response from OutputFormatNode
        routing_decision: str  — JSON routing decision, checked against the taxonomy

    Output state keys (partial dict):
        formatted_output: str
        result:           str
        status:           str
        error_log:        list[str]  (only on ERROR)
        the remaining output-bearing fields, blanked, on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def _withhold(self, state: AgentState, violation: str) -> Dict[str, Any]:
        """Return the contained error envelope for a gate violation."""
        logger.error("PostProcessNode: output gate violation — %s", violation)
        emit_trace_event("post_process_output_gate_violation", {"violation": violation}, state)
        return {
            **_cleared_output_state(),
            "formatted_output": _WITHHELD_NOTICE,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PostProcessNode: output gate withheld the response — {violation}"],
        }

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # A declined request produced no routing report. Without this branch the
        # empty-report fallback below reports "no routing decision generated —
        # check the audit trail", which sends the caller looking for an upstream
        # failure instead of telling them the one value they need to change.
        marker = state.get("error_code")
        if marker:
            emit_trace_event("output_not_produced", {"reason": marker}, state)
            return {
                **_cleared_output_state(),
                "formatted_output": _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED),
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
            }

        routing_report: str = state.get("routing_report") or ""

        # ── Fallback for an empty report ──────────────────────────────────────
        if not routing_report.strip():
            logger.warning("PostProcessNode: routing_report is empty — using fallback message")
            routing_report = (
                "[Service Request Routing] No routing decision generated. "
                "Check the audit trail for upstream failures."
            )

        # ── Invariant 1: no credential shapes in the response ─────────────────
        violation = _security_gate_output(routing_report)
        if violation:
            return self._withhold(state, violation)

        # ── Invariant 2: every destination is declared in the taxonomy ────────
        routing: Dict[str, Any] = from_json(state.get("routing_decision"), {}) or {}
        if not _routing_destinations_declared(routing):
            return self._withhold(state, _VIOLATION_UNDECLARED_ROUTE)

        logger.info("PostProcessNode: output gate passed — length=%d", len(routing_report))
        emit_trace_event("post_process_complete", {"output_length": len(routing_report)}, state)

        return {
            "formatted_output": routing_report,
            "result": routing_report,
            "status": AgentStatus.SUCCESS.value,
        }
