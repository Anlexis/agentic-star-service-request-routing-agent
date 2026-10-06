"""AgentCore Platform v1.0"""

# SVC-C2-018 — Outer graph (two-layer nested architecture).
#
# Architecture:
#
#   Outer backbone (fixed — do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, up to max_retry)
#                                          pre_process
#
#   The `main` slot is a GraphNode subclass (ServiceRoutingGraphNode) that
#   delegates the full domain workflow to DomainWorkflowGraph (inner graph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#   src/graph/context_bridge.py        ← request metadata across the boundary
#
# Structure rules this file holds to:
#   ServiceRequestClassificationRoutingAgent inherits AgentBaseGraph directly
#   super().register_nodes() is called first (fills initialize + finalize)
#   ServiceRoutingGraphNode is assigned to self._nodes["main"]
#   PreProcessNode (VERIFIED_EXTERNAL) fills the pre_process slot — the trust gate
#   PostProcessNode (ANONYMOUS) fills the post_process slot — the output gate
#   merge_output() returns only changed keys
#   the class name matches the config/agent.yaml `class` entry point exactly
#   add_edges() is NOT overridden; no platform-internal imports

from typing import Any, ClassVar, Dict, cast

from framework.schemas.agent_status import AgentStatus
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.graph.base_graph import BaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState

from src.config_loader import load_runtime_config
from src.graph.context_bridge import stash_request_context
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State, from_json


class ServiceRoutingGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of the agent.

    Wraps DomainWorkflowGraph (the inner workflow).
    Called by the backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()  — instantiate and return DomainWorkflowGraph
      extract_input() — pull validated_input (the request text) from outer state,
                        and stash the request metadata for the inner graph
      merge_output()  — map sub_result fields into the outer state delta
                        (changed keys only)
      error_strategy  — "propagate": re-raise inner errors (fail-fast)
    """

    error_strategy: ClassVar[str] = "propagate"
    propagate_hitl: ClassVar[bool] = False

    def execute(self, state: AgentState) -> Dict[str, Any]:
        """Skip the inner graph when the request was already found unacceptable.

        A request PreProcessNode declined has no validated text to classify, so
        running the workflow would only reach the first domain node, fail its own
        precondition, and replace the specific, actionable reason already settled
        with a vaguer one.
        """
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        return cast(Dict[str, Any], super().execute(state))

    def get_subgraph(self) -> BaseGraph:
        """Instantiate and return the inner domain workflow graph.

        Imported inside the method to avoid a circular import at module load.
        The runtime parameters come from config/config.yaml, so a value declared
        there reaches the inner graph rather than being replaced by a default.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config=load_runtime_config())

    def extract_input(self, state: AgentState) -> str:
        """Return the request string passed into the inner graph's invoke().

        PreProcessNode validates and normalises the raw request and writes the
        result to validated_input. The request METADATA it also wrote does not
        cross the subgraph boundary on its own — the boundary forwards only the
        string — so it is stashed here for the inner graph to seed from.
        """
        stash_request_context(from_json(state.get("enriched_context"), {}) or {})
        return str(state.get("validated_input") or state.get("user_input") or "")

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map the inner graph's sub_result back into the outer state delta.

        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "routing_decision", "routing_report",
                                       "classification_result", "status"
          This merge_output() reads → sub_result.get(...) for each of these keys.

        PostProcessNode (outer post_process) reads routing_report from state to
        apply the output gate and set formatted_output.
        """
        return {
            # A reason settled OUTSIDE the subgraph (PreProcessNode) is the real
            # one: reading sub_result alone would overwrite it with the inner
            # blank, since the inner graph never ran.
            "error_code": state.get("error_code") or sub_result.get("error_code"),
            "routing_decision": sub_result.get("routing_decision"),
            "routing_report": sub_result.get("routing_report"),
            "classification_result": sub_result.get("classification_result"),
            "status": sub_result.get("status"),
        }


class ServiceRequestClassificationRoutingAgent(AgentBaseGraph):
    """Outer graph for SVC-C2-018 — service-request classification and routing.

    Inherits AgentBaseGraph directly. Domain logic is fully encapsulated in
    ServiceRoutingGraphNode (main slot), which delegates to DomainWorkflowGraph.

    Backbone (fixed):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() is the ONLY override:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process:  PreProcessNode          (VERIFIED_EXTERNAL — trust gate)
      - main:         ServiceRoutingGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode         (ANONYMOUS — output gate)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.

    The class name MUST match the config/agent.yaml `class` entry point exactly.
    server.py imports this as `Graph` via the alias below.
    """

    def __init__(self, config: Dict[str, Any] | None = None) -> None:
        """Accept runtime parameters, defaulting to config/config.yaml.

        The framework reads max_retry (and the memory/HITL switches) off this
        mapping. Constructing the graph with no config leaves every declared
        value unread while the agent still starts and answers, so the default is
        the shipped file rather than an empty mapping.
        """
        super().__init__(config if config is not None else load_runtime_config())

    @property
    def name(self) -> str:
        """Agent identifier registered with the platform registry."""
        return "ServiceRequestClassificationRoutingAgent"

    @property
    def state_schema(self) -> type:
        return State

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = ServiceRoutingGraphNode()
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.


# Alias for the standalone entry point (server.py imports Graph).
Graph = ServiceRequestClassificationRoutingAgent
