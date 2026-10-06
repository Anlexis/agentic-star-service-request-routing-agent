# SVC-C2-018 — the output gate and what the caller receives when it refuses.
#
# The envelope builder resolves the caller-visible output as
# `formatted_output or result`, with no status check. Three consequences drive
# every assertion in this module:
#
#   1. an error path that leaves `result` populated ships the un-gated answer
#      inside the error envelope;
#   2. a FALSY formatted_output re-opens that fallback, so the withheld-notice
#      must be non-empty;
#   3. partial deltas are MERGED into state, so omitting a key leaves the old
#      value in place. `assert not result.get(field)` therefore passes on a gate
#      that clears nothing — every clearing assertion below checks PRESENCE in
#      the returned delta AND emptiness of the value.

import pytest

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from src.config_loader import DEFAULT_CATEGORIES
from src.nodes.post_process_node import (
    _OUTPUT_BEARING_FIELDS,
    _WITHHELD_NOTICE,
    PostProcessNode,
)
from src.schemas.state import to_json

CLEAN_REPORT = (
    "SERVICE REQUEST ROUTING DECISION\n"
    "Category:    it_support\n"
    "Route Queue: itsm_queue\n"
    "Handler:     IT Service Desk"
)

DECLARED_ROUTING = {
    "category": "it_support",
    "queue": "itsm_queue",
    "handler": "IT Service Desk",
    "priority": "high",
    "confidence": 0.9,
    "routed_from_config": True,
    "fallback": False,
}


@pytest.fixture(autouse=True)
def patch_emit(monkeypatch):
    monkeypatch.setattr("src.nodes.post_process_node.emit_trace_event", lambda *a, **k: None)


@pytest.fixture
def node():
    return PostProcessNode()


def _state(report, routing=None):
    return {
        "routing_report": report,
        "routing_decision": to_json(routing if routing is not None else DECLARED_ROUTING),
        "caller_trust_level": TrustLevel.ANONYMOUS.value,
    }


class TestCleanPathControl:
    """A refuse-everything gate would pass every containment test in this file.

    This is the control that stops that: the same node, on an ordinary report,
    must still produce the real answer.
    """

    def test_clean_report_is_surfaced(self, node):
        result = node(_state(CLEAN_REPORT))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["formatted_output"] == CLEAN_REPORT
        assert result["result"] == CLEAN_REPORT

    def test_empty_report_falls_back_to_a_notice_not_to_silence(self, node):
        result = node(_state(""))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "No routing decision generated" in result["formatted_output"]


class TestCredentialViolationIsContained:
    LEAKY = CLEAN_REPORT + "\nOn-call access: postgresql://svc:example_secret_value@db.internal/routing"

    def test_status_is_error(self, node):
        assert node(_state(self.LEAKY))["status"] == AgentStatus.ERROR.value

    def test_every_output_bearing_field_is_present_and_empty(self, node):
        """Presence AND emptiness — omitting a key would leave the old value in state."""
        result = node(_state(self.LEAKY))
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result, (
                f"{field} must be present in the returned delta; omitting it leaves " "the previous value in state"
            )
            assert result[field] == ""

    def test_the_released_text_appears_nowhere_in_the_delta(self, node):
        result = node(_state(self.LEAKY))
        blob = repr(result)
        assert "example_secret_value" not in blob
        assert "postgresql://" not in blob

    def test_the_notice_is_truthy(self, node):
        """A falsy replacement re-opens the `formatted_output or result` fallback."""
        result = node(_state(self.LEAKY))
        assert result["formatted_output"] == _WITHHELD_NOTICE
        assert bool(result["formatted_output"])

    def test_the_reason_names_a_label_not_the_value(self, node):
        result = node(_state(self.LEAKY))
        joined = " ".join(result["error_log"])
        assert "credential_pattern" in joined
        assert "example_secret_value" not in joined
        assert "Traceback" not in joined
        assert "/src/" not in joined

    def test_the_cleared_set_covers_every_field_that_can_carry_an_answer(self):
        """Inventory guard: a new output-bearing field cannot quietly join the state.

        The gate writes through _cleared_output_state(), so a field added to the
        node's output without being added here fails this test rather than
        silently surviving a violation.
        """
        assert set(_OUTPUT_BEARING_FIELDS) == {
            "result",
            "routing_report",
            "routing_decision",
            "classification_result",
        }


class TestAssignmentShapeThePlatformDoesNotDetect:
    """The one shape the template adds on top of the platform's own detector.

    It matters because the platform's detector fires one node earlier — inside
    OutputFormatNode, which returns the report string — for every shape it does
    recognise. This gate is therefore only ever reached by shapes the platform
    misses, and an inline secret assignment is the realistic one.
    """

    LEAKY = CLEAN_REPORT + "\nHandler mailbox password: example_secret_value"

    def test_it_is_refused_here(self, node):
        result = node(_state(self.LEAKY))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("credential_assignment" in e for e in result["error_log"])
        assert "example_secret_value" not in repr(result)


class TestUndeclaredDestinationIsContained:
    """The agent's stated guarantee, enforced at the boundary that ships it.

    Routing destinations come from configuration, never from the request. The
    fault injected here is on the DATA path — a routing decision naming a queue
    the configuration does not declare, which is what a resumed checkpoint or a
    configuration change between the two reads produces. This is a unit-level
    proof: reproducing the same divergence end to end needs the configuration to
    change mid-request.
    """

    @pytest.mark.parametrize(
        "routing",
        [
            {**DECLARED_ROUTING, "queue": "attacker_queue"},
            {**DECLARED_ROUTING, "category": "shadow_category"},
        ],
    )
    def test_undeclared_destination_is_withheld(self, node, routing):
        report = CLEAN_REPORT.replace("itsm_queue", str(routing["queue"]))
        result = node(_state(report, routing))
        assert result["status"] == AgentStatus.ERROR.value
        assert any("undeclared_routing_destination" in e for e in result["error_log"])
        assert result["formatted_output"] == _WITHHELD_NOTICE
        for field in _OUTPUT_BEARING_FIELDS:
            assert field in result and result[field] == ""

    def test_every_declared_destination_passes(self, node):
        """The other direction: the gate must not refuse the configured taxonomy."""
        for entry in DEFAULT_CATEGORIES:
            routing = {**DECLARED_ROUTING, "category": entry["id"], "queue": entry["queue"]}
            result = node(_state(CLEAN_REPORT, routing))
            assert result["status"] == AgentStatus.SUCCESS.value, entry["id"]

    def test_absent_routing_decision_is_not_treated_as_a_violation(self, node):
        result = node(_state(CLEAN_REPORT, {}))
        assert result["status"] == AgentStatus.SUCCESS.value
