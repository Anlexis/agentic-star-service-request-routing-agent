"""AgentCore Platform v1.0"""

# Manifest entry point re-export (config/agent.yaml -> module: "src.graph",
# class: "ServiceRequestClassificationRoutingAgent"). AgentRegistry resolves the
# manifest by importing `module` and reading `class` off it, so the agent class
# MUST be importable as an attribute of the `src.graph` package.
from src.graph.graph import ServiceRequestClassificationRoutingAgent

__all__ = ["ServiceRequestClassificationRoutingAgent"]
