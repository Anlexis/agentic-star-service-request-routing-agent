"""AgentCore Platform v1.0"""

# SVC-C2-018 — InputValidateNode
# Inner domain node 1: domain-level validation of the free-text service request.
#
# Distinct from PreProcessNode (trust, hostile content, boundary limits): this
# node applies the business rules — enough word content to be classifiable,
# whitespace/case normalisation, channel extraction — and writes the normalised
# service_request payload for the downstream nodes.
#
# Inner node — ANONYMOUS trust. The outer PreProcessNode already enforced caller
# trust at VERIFIED_EXTERNAL; the inner nodes must run at ANONYMOUS so the
# caller's invocation context passes through the subgraph boundary unrejected.
#
# Returns ONLY the state keys this node writes (partial-dict contract).

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import INPUT_REJECTED
from src.services.progress import emit_progress

from src.schemas.state import from_json, to_json

logger = logging.getLogger(__name__)

# A routable request needs at least this many words — a single token ("help")
# cannot be classified with any confidence.
_MIN_WORDS = 2


class InputValidateNode(FunctionNode):
    """Domain validation of the free-text service request for SVC-C2-018.

    Applies the business-rule checks beyond the structural boundary check in
    PreProcessNode: minimum word content, normalisation, channel extraction.
    Produces the normalised service_request payload.

    Inner node — ANONYMOUS trust (see module docstring).

    Input state keys:
        validated_input:  str  — normalised request text from PreProcessNode.
                                 Falls back to user_input for direct invocation.
        enriched_context: str  — JSON channel metadata; optional

    Output state keys (partial dict):
        service_request: str   — JSON-serialised normalised request payload
        status:          str
        error_log:       list[str]  (only on ERROR)
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def execute(self, state: AgentState) -> Dict[str, Any]:
        raw = state.get("validated_input") or state.get("user_input", "")
        text = " ".join(str(raw).split())

        if not text:
            emit_trace_event("input_validate_failed", {"reason": "empty_request"}, state)
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["InputValidateNode: request text is empty"],
            }

        word_count = len(text.split())
        if word_count < _MIN_WORDS:
            emit_trace_event(
                "input_validate_failed",
                {"reason": "too_few_words", "word_count": word_count},
                state,
            )
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"InputValidateNode: request has too few words to classify ({word_count})"],
            }

        # ── Channel extraction (from PreProcessNode's enriched_context) ───────
        enriched: Dict[str, Any] = from_json(state.get("enriched_context"), {}) or {}
        channel = str(enriched.get("channel", "unknown"))

        service_request: Dict[str, Any] = {
            "text": text,
            "normalised_text": text.lower(),
            "channel": channel,
            "word_count": word_count,
            "length": len(text),
        }

        logger.info("InputValidateNode: normalised request words=%d channel=%s", word_count, channel)
        emit_trace_event(
            "input_validate_complete",
            {"word_count": word_count, "channel": channel, "length": len(text)},
            state,
        )

        return {
            "service_request": to_json(service_request),
            "status": AgentStatus.SUCCESS.value,
        }
