"""AgentCore Platform v1.0"""

# SVC-C2-018 — DomainWorkflowGraph (inner graph)
#
# The inner half of the two-layer nested architecture. It encapsulates the
# service-request classification & routing pipeline:
#
#   START
#     → input_validate      (InputValidateNode)
#     → build_context       (BuildContextNode)
#     → generate_response   (GenerateResponseNode — classify)
#     → route_resolve       (RouteResolveNode — config-taxonomy routing)
#     → output_format       (OutputFormatNode)
#     → END
#
# Called by ServiceRoutingGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Structure rules this file holds to:
#   inherits BaseGraph (fully custom topology — no forced backbone)
#   implements every BaseGraph abstract method
#   register_nodes() does NOT call super() (it is abstract on BaseGraph)
#   does NOT register initialize / finalize (outer backbone concerns)
#   every inner node declares required_trust_level = TrustLevel.ANONYMOUS
#   get_output() is designed together with ServiceRoutingGraphNode.merge_output()
#   every inner node constructor takes no arguments
#   no platform-internal imports

from typing import Any, Dict

from langgraph.graph import END, START

from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus

from src.graph.context_bridge import read_request_context
from src.nodes.build_context_node import BuildContextNode
from src.nodes.generate_response_node import GenerateResponseNode
from src.nodes.input_validate_node import InputValidateNode
from src.nodes.output_format_node import OutputFormatNode
from src.nodes.route_resolve_node import RouteResolveNode
from src.schemas.state import State, to_json


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for SVC-C2-018.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by ServiceRoutingGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → input_validate     (InputValidateNode)
          → build_context      (BuildContextNode)
          → generate_response  (GenerateResponseNode — classify)
          → route_resolve      (RouteResolveNode — config-taxonomy routing)
          → output_format      (OutputFormatNode)
          → END

    All nodes run at ANONYMOUS trust; the caller's trust was already enforced by
    the outer PreProcessNode. initialize / finalize are outer backbone concerns
    and are not registered here.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "svc_c2_018_service_request_routing_workflow"

    @property
    def state_schema(self) -> type:
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """No mandatory runtime parameters — the taxonomy has a built-in default."""
        pass

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the request metadata the subgraph boundary does not forward.

        The boundary passes the request STRING only, so anything the outer nodes
        wrote — the caller's channel among it — would otherwise be absent here
        and every reader would silently fall back to "unknown". The outer node
        stashes the metadata immediately before delegating; this reads it back.
        """
        context = read_request_context()
        return {"enriched_context": to_json(context)} if context else {}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 5 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        """
        self._nodes["input_validate"] = InputValidateNode()
        self._nodes["build_context"] = BuildContextNode()
        self._nodes["generate_response"] = GenerateResponseNode()
        self._nodes["route_resolve"] = RouteResolveNode()
        self._nodes["output_format"] = OutputFormatNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear classification & routing topology.

        Linear flow:
            input_validate → build_context → generate_response
            → route_resolve → output_format → END.

        There is no conditional branching: every path through the routing
        pipeline is linear, so route() satisfies the abstract method but is
        never reached at runtime.
        """
        self._sg.add_edge(START, "input_validate")
        self._sg.add_edge("input_validate", "build_context")
        self._sg.add_edge("build_context", "generate_response")
        self._sg.add_edge("generate_response", "route_resolve")
        self._sg.add_edge("route_resolve", "output_format")
        self._sg.add_edge("output_format", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Conditional routing — required by the BaseGraph contract.

        The topology is linear and add_conditional_edges() is not used, so this
        method is never called at runtime. It is annotated with this graph's own
        State because the graph library reads a path callable's annotation as
        its input schema and projects away every field the annotation does not
        declare — an annotation naming a wider base type would make the routing
        fields invisible here if the topology ever gained a branch.
        Returns END on error so an unexpected call cannot re-enter a node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return END
        return "output_format"

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: State) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        This dict is received by ServiceRoutingGraphNode.merge_output() in
        graph.py as the `sub_result` argument. Both methods are designed
        together so the field names cannot drift:

            Inner get_output() emits:   "routing_decision", "routing_report",
                                        "classification_result", "status"
            Outer merge_output() reads: sub_result.get(...) for each key above.

        Every key is read straight out of state with no `or` fallback: a
        fallback would re-surface a pre-gate value whenever the gated one came
        back empty, which is the failure mode the output gate exists to prevent.
        """
        return {
            # The reason must leave the subgraph or the outer graph has no way
            # to tell a declined request from a produced-nothing one.
            "error_code": state.get("error_code"),
            "routing_decision": state.get("routing_decision"),
            "routing_report": state.get("routing_report"),
            "classification_result": state.get("classification_result"),
            "status": state.get("status"),
            "node_history": state.get("node_history", []),
            "correlation_id": state.get("correlation_id"),
        }
