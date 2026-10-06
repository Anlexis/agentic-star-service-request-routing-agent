"""AgentCore Platform v1.0"""

# SVC-C2-018 — PreProcessNode
# Outer backbone pre_process slot: the trust gate plus request validation.
#
# Responsibilities:
#   - enforce VERIFIED_EXTERNAL caller trust (required_trust_level)
#   - refuse hostile request text: chat-template control tokens and directives
#     aimed at the agent's own instructions
#   - refuse a request whose text the platform privacy filter removed entirely,
#     with a reason the caller can act on
#   - refuse a request carrying a credential, naming the field and not the value
#   - reject empty / oversized requests early (fail-fast)
#   - normalise whitespace and write validated_input + enriched_context to State
#   - emit an audit event for every validation decision
#
# The incoming request is FREE TEXT (a service ticket / complaint / HR ask),
# not structured data — classification happens downstream in the domain workflow.
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

from src.schemas.state import to_json
from src.screens import (
    ALLOWED_CONTEXT_KEYS,
    credential_findings,
    screen_structure,
    screen_text,
    text_is_fully_masked,
)

logger = logging.getLogger(__name__)

# Bound the accepted free-text request length (chars). Anything longer is
# almost certainly not a routable single request; reject at the boundary.
_MAX_REQUEST_CHARS = 20000
_MIN_REQUEST_CHARS = 3


class PreProcessNode(FunctionNode):
    """Request validation for SVC-C2-018.

    Validates the caller-supplied free-text service request before the domain
    workflow runs. This is the outer backbone's pre_process slot — the only node
    that requires VERIFIED_EXTERNAL trust, so unauthenticated or anonymous
    callers are rejected here (fail-fast; the inner domain nodes run at
    ANONYMOUS and never see untrusted input directly).

    Input state keys:
        user_input:    str  — caller-supplied free-text service request
        input_context: dict — validated request metadata (channel)

    Output state keys (partial dict):
        validated_input:  str        — whitespace-normalised request text
        enriched_context: str        — JSON-serialised channel metadata
        status:           str        — AgentStatus.SUCCESS or ERROR
        error_log:        list[str]  — set only on ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def _refuse(
        self, state: AgentState, reason: str, message: str, code: str = "INVALID_REQUEST", **payload: object
    ) -> Dict[str, Any]:
        """Return a refusal that names the reason but never the rejected value.

        Two outcomes, chosen by whether the caller can act on the finding, and
        selected by an explicit argument at the call site rather than by the text
        of `reason` or `message` — so the distinction survives any later
        rewording of either.

        `code` non-empty — a value the caller can correct (nothing sent, too
        short, too long). The run COMPLETES carrying the reason, so the calling
        surface can show the sentence and the caller can send a corrected request
        on the same conversation instead of receiving only an exception type.

        `code` empty — a refusal the caller cannot reword their way past: spliced
        instructions, a credential in the request, or a request the privacy
        filter removed in full. These terminate, exactly as before.

        Either way the request is NOT classified and nothing is published; only
        the way the refusal is reported changes.
        """
        emit_trace_event("pre_process_validation_failed", {"reason": reason, **payload}, state)
        if code:
            emit_progress(INPUT_REJECTED)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": code,
                "error_log": [f"PreProcessNode: {message}"],
            }
        return {
            "status": AgentStatus.ERROR.value,
            "error_log": [f"PreProcessNode: {message}"],
        }

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        raw_context = state.get("input_context", {}) or {}
        input_context = {key: value for key, value in raw_context.items() if key in ALLOWED_CONTEXT_KEYS}

        # ── Emptiness check ───────────────────────────────────────────────────
        if not user_input or not isinstance(user_input, str) or not user_input.strip():
            logger.warning("PreProcessNode: user_input is empty or missing")
            return self._refuse(state, "empty_input", "user_input is empty or missing", code="EMPTY_INPUT")

        # ── Hostile-content screen ────────────────────────────────────────────
        # Runs before anything else looks at the text. The platform input gate
        # refuses some injection forms, but not chat-template control tokens
        # written as <<SYS>>, and not a directive spliced with markup — so the
        # template screens for the whole class itself rather than relying on the
        # gate in front of it.
        label = screen_text(user_input)
        if label:
            logger.warning("PreProcessNode: request refused by the content screen — %s", label)
            # code="" keeps this one terminal. Spliced instructions are not a
            # value the caller can correct by rewording, and completing the run
            # would make a refusal read like an ordinary declined request.
            return self._refuse(
                state,
                "hostile_content",
                f"request refused by the content screen ({label})",
                code="",
                screen=label,
            )
        found = screen_structure(input_context)
        if found:
            # Terminal for the same reason as the text screen above.
            return self._refuse(
                state,
                "hostile_context",
                f"input_context.{found[0]} refused by the content screen ({found[1]})",
                code="",
                screen=found[1],
            )

        # ── Credential screen ─────────────────────────────────────────────────
        # A credential-shaped string in the request text would be returned into
        # this node's own result as validated_input, where the platform output
        # gate finds it and aborts the run with a message the caller cannot act
        # on. The platform's own detector is used so the refusal set matches the
        # block set exactly. Only the finding type is reported, never the value.
        findings = credential_findings(user_input)
        if findings:
            # Terminal: a credential reaching the request is a containment
            # event, not a formatting mistake to correct.
            return self._refuse(
                state,
                "credential_in_request",
                "request text appears to contain a credential; remove it and resubmit",
                code="",
                finding=findings[0],
            )

        # ── Normalise whitespace ──────────────────────────────────────────────
        normalised = " ".join(user_input.split())

        # ── Privacy filter removed the whole request ──────────────────────────
        # The platform masks personal data in the request before this node runs,
        # and its name pattern matches long runs of capitalised words — so an
        # ordinary title-cased ticket arrives here as nothing but placeholders.
        # Classifying what is left would either produce a confident routing
        # decision from no content or fail several nodes later for an unrelated
        # reason, so the condition is reported as itself.
        if text_is_fully_masked(normalised):
            logger.warning("PreProcessNode: request text was removed in full by the privacy filter")
            # Terminal, deliberately. This condition is downstream of the
            # platform's personal-data masking, so it is held to the same
            # treatment as the other findings on that path rather than being
            # relaxed alongside the ordinary bounds checks below.
            return self._refuse(
                state,
                "fully_redacted",
                "the request text was removed in full by the privacy filter — resubmit "
                "describing the issue without personal identifiers",
                code="",
            )

        # ── Length bounds ─────────────────────────────────────────────────────
        if len(normalised) < _MIN_REQUEST_CHARS:
            return self._refuse(
                state,
                "too_short",
                f"request too short to classify ({len(normalised)} chars)",
                code="INVALID_REQUEST",
                length=len(normalised),
            )
        if len(normalised) > _MAX_REQUEST_CHARS:
            # The whole request is oversized, not one field of it.
            return self._refuse(
                state,
                "too_long",
                f"request exceeds {_MAX_REQUEST_CHARS} chars ({len(normalised)} chars)",
                code="QUESTION_TOO_LONG",
                length=len(normalised),
            )

        # ── Success ───────────────────────────────────────────────────────────
        channel = str(input_context.get("channel", "unknown"))
        logger.info("PreProcessNode: validated request length=%d channel=%s", len(normalised), channel)
        emit_trace_event(
            "pre_process_validated",
            {"request_length": len(normalised), "channel": channel},
            state,
        )

        return {
            "validated_input": normalised,
            "enriched_context": to_json(
                {
                    "source": "ServiceRequestClassificationRoutingAgent",
                    "channel": channel,
                    "length": len(normalised),
                }
            ),
            "status": AgentStatus.SUCCESS.value,
        }
