"""AgentCore Platform v1.0"""

# SVC-C2-018 — GenerateResponseNode
# Inner domain node 3: classify the service request into a taxonomy category,
# assign a priority, and produce a confidence score.
#
# Classification is deterministic — keyword signals scored against the ALLOWED
# categories carried in classification_context, which BuildContextNode loaded
# from the configured taxonomy. No language model is invoked; the manifest
# declares `generation_mode: deterministic` to say so.
#
# The classifier can ONLY emit a category id that exists in the configured
# taxonomy; it never invents a routing destination. Routing itself is resolved
# downstream in RouteResolveNode, strictly from that taxonomy.
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

# Keyword signals per category id. Keys MUST be a subset of the taxonomy ids in
# src/config_loader.py. Substring match on the lowercased request.
_CATEGORY_SIGNALS: Dict[str, List[str]] = {
    "it_support": [
        "password",
        "login",
        "log in",
        "vpn",
        "laptop",
        "software",
        "email",
        "network",
        "server",
        "access",
        "system",
        "computer",
        "printer",
        "wifi",
        "wi-fi",
        "reset",
        "account locked",
        "application",
    ],
    "hr_request": [
        "leave",
        "payroll",
        "salary",
        "benefits",
        "onboarding",
        "vacation",
        "pto",
        "holiday",
        "contract",
        "employee",
        "recruitment",
        "annual leave",
        "sick leave",
        "timesheet",
    ],
    "facilities": [
        "office",
        "desk",
        "aircon",
        "air conditioning",
        "cleaning",
        "meeting room",
        "building",
        "elevator",
        "lighting",
        "maintenance",
        "repair",
        "furniture",
        "parking",
        "badge",
        "hvac",
    ],
    "finance_billing": [
        "invoice",
        "billing",
        "payment",
        "refund",
        "expense",
        "reimbursement",
        "charge",
        "receipt",
        "budget",
        "purchase order",
        "vendor",
        "po number",
    ],
    "customer_complaint": [
        "complaint",
        "unhappy",
        "dissatisfied",
        "poor service",
        "escalate",
        "cancel my",
        "disappointed",
        "unacceptable",
        "terrible",
        "worst",
        "not happy",
        "want a refund",
    ],
}

# Priority escalation signals — bump the category default priority upward.
_URGENCY_SIGNALS: Dict[str, List[str]] = {
    "critical": [
        "outage",
        "system down",
        "is down",
        "breach",
        "security incident",
        "cannot access",
        "can't access",
        "urgent",
        "emergency",
        "critical",
        "asap",
    ],
    "high": [
        "blocked",
        "not working",
        "failed",
        "escalate",
        "important",
        "deadline",
        "immediately",
        "high priority",
    ],
}

_PRIORITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _score_categories(text: str, allowed_ids: List[str]) -> Dict[str, List[str]]:
    """Return {category_id: [matched signals]} for every allowed category."""
    matches: Dict[str, List[str]] = {}
    for cat_id in allowed_ids:
        signals = _CATEGORY_SIGNALS.get(cat_id, [])
        hit = [s for s in signals if s in text]
        if hit:
            matches[cat_id] = hit
    return matches


def _detect_priority(text: str, base_priority: str) -> str:
    """Escalate the base (category default) priority using urgency signals."""
    best = base_priority
    for level, signals in _URGENCY_SIGNALS.items():
        if any(s in text for s in signals):
            if _PRIORITY_RANK.get(level, 0) > _PRIORITY_RANK.get(best, 0):
                best = level
    return best


class GenerateResponseNode(FunctionNode):
    """Classify the request into a taxonomy category with priority + confidence.

    Deterministic keyword-signal classification — no language model is invoked.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        classification_context: str  — JSON context from BuildContextNode

    Output state keys (partial dict):
        classification_result: str   — JSON classification
        status:                str
        error_log:             list[str]  (only on ERROR)
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

        context: Dict[str, Any] = from_json(state.get("classification_context"), {})

        if not context or not context.get("categories"):
            emit_trace_event(
                "generate_response_failed",
                {"reason": "missing_classification_context"},
                state,
            )
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["GenerateResponseNode: classification_context missing in state"],
            }

        categories: List[Dict[str, Any]] = context["categories"]
        allowed_ids = [c["id"] for c in categories]
        default_id = context.get("default_category", allowed_ids[-1])
        by_id = {c["id"]: c for c in categories}

        text = str(context.get("request_text", "")).lower()

        # ── Classify (constrained to allowed taxonomy ids) ────────────────────
        matches = _score_categories(text, allowed_ids)
        if matches:
            # Winner = category with the most matched signals (stable by id order).
            category = max(matches, key=lambda cid: (len(matches[cid]), -allowed_ids.index(cid)))
            matched_signals = matches[category]
            # Confidence grows with match strength; capped.
            confidence = min(0.5 + 0.1 * len(matched_signals), 0.95)
        else:
            category = default_id
            matched_signals = []
            confidence = 0.3

        base_priority = str(by_id.get(category, {}).get("default_priority", "low"))
        priority = _detect_priority(text, base_priority)

        rationale = (
            f"Matched {len(matched_signals)} signal(s) for category '{category}'"
            if matched_signals
            else f"No category signals matched; defaulted to '{category}'"
        )

        classification_result: Dict[str, Any] = {
            "category": category,
            "priority": priority,
            "confidence": round(confidence, 2),
            "matched_signals": matched_signals,
            "rationale": rationale,
        }

        logger.info(
            "GenerateResponseNode: category=%s priority=%s confidence=%.2f signals=%d",
            category,
            priority,
            confidence,
            len(matched_signals),
        )
        emit_trace_event(
            "generate_response_complete",
            {
                "category": category,
                "priority": priority,
                "confidence": round(confidence, 2),
                "signal_count": len(matched_signals),
            },
            state,
        )

        return {
            "classification_result": to_json(classification_result),
            "status": AgentStatus.SUCCESS.value,
        }
