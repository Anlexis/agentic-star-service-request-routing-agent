# Service Request Classification & Routing Agent

AI agent for classifying and routing service requests, built with Agentic Star.

> **Category**: Cat 2 (domain pipeline — a multi-step job to be done)
> **Industry**: Services
> **Template ID**: SVC-C2-018

## Overview

Routes free-text service requests to the right queue.

A shared-service desk receives requests through several channels at once — IT
tickets, HR asks, facilities reports, billing queries, complaints — and someone
has to read each one and decide where it belongs. This agent does that first
step: it reads the request, classifies it into one of a configured set of
categories, assigns a priority, and resolves the target queue and handler from
configuration. It answers with a short routing document recording the decision,
the confidence, and the signals behind it.

```
============================================================
SERVICE REQUEST ROUTING DECISION
============================================================
Channel:     email
Category:    it_support
Route Queue: itsm_queue
Handler:     IT Service Desk
Priority:    CRITICAL
Confidence:  95%
Fallback:    NO

Classification Rationale:
  Matched 5 signal(s) for category 'it_support'
  Matched signals: password, vpn, laptop, access, reset

Routing source: configured taxonomy (routed_from_config=True)
============================================================
```

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | Supplies the secret provider, the audit sink and the gateway the agent is designed to run behind. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from the package registry as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## How it classifies

Deterministically. Keyword signals are scored against the configured taxonomy —
no language model is invoked, and the manifest declares
`generation_mode: deterministic`.

That is a design choice, not a placeholder. A routing decision is an action with
a real cost when it is wrong, and a deterministic classifier is reproducible,
fully testable, side-effect-free, and structurally incapable of inventing a
destination that does not exist.

## The routing table is yours

Categories, queues, handlers and default priorities live in `config/config.yaml`.
Change them for your own service desk and restart — no code change:

```yaml
routing:
  taxonomy_version: "svc-routing-v1"
  default_category: "general_inquiry"
  categories:
    - id: "it_support"
      label: "IT Support"
      queue: "itsm_queue"
      handler: "IT Service Desk"
      default_priority: "medium"
```

Destinations exist **only** in this table. The classifier chooses among the ids
declared here and nothing else, a queue name is never read from the request text,
and the output boundary refuses any response whose destination is not declared.
An unknown or injected category id is treated as a lookup miss and falls back to
the default queue.

## Using it

```bash
uvicorn src.api.server:app --port 8000
```

```bash
curl -X POST localhost:8000/invoke \
  -H "Authorization: Bearer $INVOKE_AUTH_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"input": "My laptop cannot connect to the VPN and I am blocked from my email.",
       "input_context": {"channel": "email"}}'
```

`input_context` is optional; only `channel` is accepted, as a lowercase
identifier. Any other key is dropped before the request is processed.

**Set `INVOKE_AUTH_TOKEN`.** Without it callers stay anonymous and every request
is refused at the first node — the agent starts, answers `/health`, and serves
nothing.

## What it refuses, and why

- **Anonymous callers** — the request boundary requires a verified caller.
- **Chat-template control tokens and instruction-override directives**, screened
  both raw and with markup removed, over values and keys. Ordinary language is
  unaffected: "Please ignore the previous ticket I raised" routes normally.
- **Requests carrying a credential.** Such a request cannot succeed — the
  framework's own output scan aborts it deep inside the pipeline — so it is
  refused at the boundary instead, naming the field and never the value.
- **Requests the privacy filter removes in full.** The platform masks personal
  data before the first node runs, and a fully title-cased sentence can be
  removed entirely; rather than classify what is left, the agent says so.

Refusals name a closed-set reason and never echo the rejected text.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

The test suite runs without a platform connection, including the tests that drive
the HTTP entry point in-process.

## Project Structure

```
src/api/server.py                   HTTP entry point (adapter only)
src/graph/graph.py                  outer graph — trust gate, delegation, output gate
src/graph/domain_workflow_graph.py  inner pipeline
src/graph/context_bridge.py         request metadata across the subgraph boundary
src/nodes/                          the seven nodes
src/config_loader.py                runtime parameters + routing taxonomy
src/screens.py                      caller-input screens
config/agent.yaml                   static manifest
config/config.yaml                  runtime parameters
tests/                              unit and boundary tests
docs/                               design and operational documentation
```

Documentation:

- `docs/02_design.md` — architecture, caller contract, output boundary
- `docs/03_test_spec.md` — test strategy and coverage map

## Customising

1. Adjust `config/config.yaml` — the routing taxonomy is the usual starting point.
2. Extend the keyword signals in `src/nodes/generate_response_node.py` to match
   how your callers actually write.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
