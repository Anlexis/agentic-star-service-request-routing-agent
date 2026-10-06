# SVC-C2-018 — the runtime configuration is READ, and reading it changes behaviour.
#
# A declared value that nothing reads looks like configuration and behaves like a
# comment: the agent starts, answers, and quietly runs on framework defaults.
# Every assertion here is about a value making a difference somewhere
# observable — the last one drives a changed taxonomy through the whole graph
# and reads the routing decision back out.

from pathlib import Path

import pytest
import yaml

from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext, TrustLevel
from src import config_loader
from src.config_loader import (
    CONFIG_PATH,
    DEFAULT_CATEGORIES,
    load_routing_taxonomy,
    load_runtime_config,
)

REQUEST = (
    "My work laptop cannot connect to the corporate VPN and I am blocked "
    "from accessing my email. Please reset my network access as soon as possible."
)


class TestShippedConfigFile:
    def test_config_yaml_ships_and_parses(self):
        assert CONFIG_PATH.exists(), "config/config.yaml must ship with the template"
        assert isinstance(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")), dict)

    def test_declared_keys_all_have_a_reader(self):
        """No key is declared for a future version.

        max_retry is read by the framework backbone, timeout_s and security by
        the deployment contract, routing by src/config_loader.py. A key outside
        that set is either wired up or removed — it is not left in the shipped
        file looking configurable.
        """
        declared = set(load_runtime_config())
        assert declared == {"max_retry", "timeout_s", "security", "routing"}

    def test_manifest_carries_no_runtime_block(self):
        """The manifest is static identity only.

        A reader pointed at an `agent.config` block in the manifest would get an
        empty mapping and degrade to defaults with no signal, which is the shape
        this split exists to prevent.
        """
        manifest = yaml.safe_load((CONFIG_PATH.parent / "agent.yaml").read_text(encoding="utf-8"))
        assert "agent" not in manifest
        assert "config" not in manifest


class TestGraphReceivesTheConfig:
    def test_agent_defaults_to_the_shipped_file(self):
        from src.graph.graph import ServiceRequestClassificationRoutingAgent

        agent = ServiceRequestClassificationRoutingAgent()
        assert agent.config == load_runtime_config()
        assert agent.config["max_retry"] == 3

    def test_max_retry_reaches_the_framework(self):
        """The framework validates max_retry off the graph's config at compile time.

        Constructing with an invalid value must therefore raise — which is what
        proves the mapping is genuinely the one the framework reads, rather than
        one the template merely holds.
        """
        from framework.errors import ConfigError
        from src.graph.graph import ServiceRequestClassificationRoutingAgent

        with pytest.raises(ConfigError, match="max_retry"):
            ServiceRequestClassificationRoutingAgent(config={"max_retry": -1}).compile()

    def test_inner_graph_is_constructed_with_the_config(self):
        from src.graph.graph import ServiceRoutingGraphNode

        subgraph = ServiceRoutingGraphNode().get_subgraph()
        assert subgraph.config == load_runtime_config()


class TestTaxonomyLoading:
    def test_shipped_file_is_the_source(self):
        taxonomy = load_routing_taxonomy()
        assert taxonomy["source"] == "config"
        assert [c["id"] for c in taxonomy["categories"]] == [c["id"] for c in DEFAULT_CATEGORIES]

    def test_absent_file_falls_back_to_the_built_in_default(self, tmp_path):
        taxonomy = load_routing_taxonomy(tmp_path / "does-not-exist.yaml")
        assert taxonomy["source"] == "builtin_default"
        assert taxonomy["categories"] == DEFAULT_CATEGORIES

    @pytest.mark.parametrize(
        "body",
        [
            "routing: {}",
            "routing:\n  categories: []",
            "routing:\n  categories:\n    - label: no id or queue",
            "not a mapping",
        ],
    )
    def test_malformed_routing_falls_back_rather_than_raising(self, tmp_path, body):
        path = tmp_path / "config.yaml"
        path.write_text(body, encoding="utf-8")
        assert load_routing_taxonomy(path)["source"] == "builtin_default"


class TestDeclaredValueChangesBehaviourEndToEnd:
    """The proof that matters: change the file, get a different routing decision.

    Everything above shows the value is read. This drives a changed taxonomy
    through the full backbone at the real caller trust level and reads the
    rendered document back, so a future refactor that quietly stops consulting
    the file fails here rather than degrading in silence.
    """

    @pytest.fixture
    def redirected_config(self, tmp_path, monkeypatch):
        original = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
        original["routing"]["categories"][0]["queue"] = "relocated_itsm_queue"
        original["routing"]["categories"][0]["handler"] = "Relocated IT Desk"
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(original, sort_keys=False), encoding="utf-8")
        monkeypatch.setattr(config_loader, "CONFIG_PATH", Path(path))
        return path

    def _invoke(self, monkeypatch):
        for module in (
            "pre_process_node",
            "input_validate_node",
            "build_context_node",
            "generate_response_node",
            "route_resolve_node",
            "output_format_node",
            "post_process_node",
        ):
            monkeypatch.setattr(f"src.nodes.{module}.emit_trace_event", lambda *a, **k: None)
        from src.graph.graph import Graph

        agent = Graph()
        agent.compile()
        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        return agent.invoke(REQUEST, ctx=ctx)

    def test_shipped_queue_is_rendered_by_default(self, monkeypatch):
        result = self._invoke(monkeypatch)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Route Queue: itsm_queue" in result["output"]

    def test_changed_queue_is_rendered_instead(self, monkeypatch, redirected_config):
        result = self._invoke(monkeypatch)
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "Route Queue: relocated_itsm_queue" in result["output"]
        assert "Handler:     Relocated IT Desk" in result["output"]
        assert "Route Queue: itsm_queue" not in result["output"]
        assert "Handler:     IT Service Desk" not in result["output"]
