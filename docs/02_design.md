# Template Design Specification — SVC-C2-018

Service Request Classification & Routing Agent (Cat 2).
Classifies a free-text service request (IT ticket, HR ask, facilities,
finance/billing, complaint) into a configured taxonomy category and routes it to
the correct queue/handler with a priority and confidence score.

**Scope:** classification is **deterministic and keyword-based**, constrained to
the configured taxonomy — **no language model is invoked**. The manifest declares
`generation_mode: deterministic` to say so.

## Position in AgentCore Architecture

| Layer | Value |
|-------|-------|
| Agent class | ServiceRequestClassificationRoutingAgent |
| L1 Base (framework base class) | AgentBaseGraph — direct framework inheritance |
| Pattern | Chat pattern lineage — taxonomy-driven routing, no model invocation |

- **Three-Layer Separation**:
  - State: flat TypedDict composition (no Pydantic — msgpack incompatible)
  - Node: framework inheritance (Template Method: `execute(self, state) -> dict` override only)
  - Graph: composition (`register_nodes()` for node substitution)

### Nested Cat 2 structure

```
src/graph/graph.py                  ← outer AgentBaseGraph
  pre_process  = PreProcessNode           (trust gate, VERIFIED_EXTERNAL)
  main         = ServiceRoutingGraphNode  (GraphNode → inner graph)
  post_process = PostProcessNode          (output gate, ANONYMOUS)

src/graph/domain_workflow_graph.py  ← inner BaseGraph (domain pipeline)
  input_validate → build_context → generate_response → route_resolve → output_format

src/graph/context_bridge.py         ← request metadata across the subgraph boundary
src/config_loader.py                ← runtime parameters + routing taxonomy
src/screens.py                      ← caller-input screens shared by the adapter and pre_process
```

### Configuration split

| File | Contents | Read by |
|------|----------|---------|
| `config/agent.yaml` | flat static manifest — identity, entry point, declared secrets/extras, required trust level | the platform registry |
| `config/config.yaml` | runtime parameters — `max_retry`, `timeout_s`, `security`, `routing` | `src/config_loader.py`, and the framework via `Graph(config=...)` |

The manifest has no nested `agent:` block. A reader pointed at one would receive
an empty mapping and degrade to defaults with no signal, so every runtime read in
this template goes through `src/config_loader.py` and no code reads the manifest.

`requires.secrets` is `[]`: this template calls no secret accessor. Declaring an
unprovisioned secret would make the agent fail at compile time.

## Architecture Overview

### Node Configuration

| Node | Layer | Responsibility | Input State | Output State | Inherits/Overrides |
|------|-------|---------------|-------------|--------------|-------------------|
| initialize | outer | session/trust setup | user_input | caller_trust_level | InitializeNode (default) |
| pre_process | outer | trust gate, hostile-content + credential + privacy-filter screens, length validation, normalise | user_input, input_context | validated_input, enriched_context, error_code | PreProcessNode (FunctionNode) |
| main | outer | delegate to inner domain graph; stash request metadata for it | validated_input, enriched_context, error_code | routing_decision, routing_report, classification_result, error_code | ServiceRoutingGraphNode (GraphNode) |
| post_process | outer | output gate: credential scan + taxonomy invariant; expose or withhold the result | routing_report, routing_decision | formatted_output, result | PostProcessNode (FunctionNode) |
| finalize | outer | response metadata | * | output | FinalizeNode (default) |
| input_validate | inner | domain validation (min words, channel) | validated_input, enriched_context | service_request, error_code | InputValidateNode |
| build_context | inner | load configured taxonomy → context | service_request | classification_context | BuildContextNode |
| generate_response | inner | classify → category/priority/confidence (deterministic keyword signals) | classification_context | classification_result | GenerateResponseNode |
| route_resolve | inner | map category → queue (configured taxonomy ONLY) | classification_result, classification_context | routing_decision | RouteResolveNode |
| output_format | inner | assemble routing response | routing_decision, classification_result, service_request | routing_report | OutputFormatNode |

`output_format` writes `routing_report` only. `result` is written **exclusively**
by `post_process`, and only in lock-step with a truthy `formatted_output` — see
"Output boundary" below.

### Data Flow

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                     ↓ (retry)
                                  pre_process

main (ServiceRoutingGraphNode) delegates to DomainWorkflowGraph:
  input_validate → build_context → generate_response → route_resolve → output_format
```

A request `pre_process` declined for a correctable value carries an `error_code`
instead of validated text: `main` returns it unchanged rather than delegating,
and `post_process` renders the caller-facing sentence — see "Rejection
contract".

### State Definition

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| validated_input | NotRequired[Optional[str]] | normalised free-text request (PreProcess) | no |
| enriched_context | NotRequired[Optional[str]] | JSON channel metadata | no |
| service_request | NotRequired[Optional[str]] | JSON normalised request payload | no |
| classification_context | NotRequired[Optional[str]] | JSON configured taxonomy + request text | no |
| classification_result | NotRequired[Optional[str]] | JSON {category, priority, confidence, ...} | no |
| routing_decision | NotRequired[Optional[str]] | JSON {category, queue, handler, priority, ...} | no |
| routing_report | NotRequired[Optional[str]] | formatted routing response text | no |
| result | NotRequired[Optional[str]] | caller-facing result (PostProcess) | no |
| error_code | Optional[str] | closed-set reason a run completed without carrying out the request; never caller content, absent on an ordinary run | no |

**State Constraints (mandatory):**
- Flat TypedDict only (primitives + JSON-serialized types via to_json/from_json)
- No JWT, API keys, credentials in State (checkpoint DB leakage)
- No Pydantic models, dataclass, arbitrary Python objects (msgpack incompatible)
- `formatted_output` is inherited from AgentState — not re-declared with a bare type

## Framework Utilization

### Caller contract

`POST /invoke` accepts:

| Field | Type | Rule |
|-------|------|------|
| `input` | str | free-text service request, 3–20,000 characters |
| `session_id` | str | optional caller-supplied conversation id |
| `input_context` | object | optional; **only** `channel` is accepted, and only as `[a-z0-9_]{1,32}` |

Undeclared `input_context` keys are **dropped** before the graph is invoked, not
merely ignored. Ignoring is not stripping: an undeclared key stays in state,
reaches the first node's result, and is scanned there — so a declared contract
only means something if the adapter removes what it does not declare.

The adapter's own refusals answer **400** with a single-string `detail`. The
validation layer owns **422** and answers there with a list of error objects;
reusing it would make both ambiguous for a client. Which refusals the adapter
owns, and which the pipeline answers with a completed run, is the next section.

### Rejection contract

A request can be turned down for two different kinds of reason, and the two do
not end the same way.

**A value the caller can correct completes the run.** Nothing was sent, the
request is shorter than 3 characters or longer than 20,000, or it carries too
few words to classify: `PreProcessNode` — or, inside the workflow,
`InputValidateNode` — returns `status: success` together with a closed-set
`error_code` (`EMPTY_INPUT`, `QUESTION_TOO_LONG`, `INVALID_REQUEST`). Every node
after the one that set it does no work and passes the code on, the inner graph
is skipped, and `PostProcessNode` turns the code into one fixed sentence from
`src/services/failure_message.py`. Terminating instead ends the calling
surface's turn and hands back only an exception type, which leaves the one value
the caller needs to change reachable solely from the audit trail; completing
lets them correct it and send the request again on the same conversation.

**A refusal the caller cannot reword their way past terminates.** A spliced
instruction or chat-template control token, a credential in the request, a
request the privacy filter removed in full, an upstream state key the next node
requires and did not receive, and an output-gate violation all return
`status: error`. Completing these would make a refusal read like an ordinary
declined request, and the output-gate case additionally has to clear what it
withheld — see "Output boundary".

In `PreProcessNode`, which owns cases of both kinds, the outcome is selected by
an explicit argument at the call site rather than by the wording of the reason,
so rewording a message cannot silently move a condition from one half to the
other.

`error_code` is deliberately not part of the envelope: `get_output()` surfaces
`output`, `status`, `trace_id`, `correlation_id` and `node_history` only. The
reason reaches the caller as the sentence and nothing else. A code in the
envelope would be a second, parallel contract to keep in step with the first,
and would invite callers to branch on a string naming an internal condition.

Those are the outcomes of the graph. The adapter screens the hostile-content,
credential, privacy-filter, oversized-body and malformed-context classes
**before** the graph is invoked, so those reach an HTTP caller as a 400 rather
than as an error envelope; `QUESTION_TOO_LONG` is what a caller reaching
`invoke()` without this adapter receives for the same oversize condition. A
correctable value passes the adapter untouched and is answered by the pipeline
with 200 and the sentence.

### Security

- **Trust**: `PreProcessNode.required_trust_level = VERIFIED_EXTERNAL` — the only
  external gate. Inner domain nodes declare `TrustLevel.ANONYMOUS` so the caller's
  invocation context passes the subgraph boundary without rejection.
- **Input**: the framework's default input gate runs first. It does **not** cover
  the whole chat-template control-token class — a payload written as `<<SYS>>`
  produces no high-confidence finding — and it does not see a directive spliced
  with markup. `src/screens.py` therefore screens the class itself, raw **and**
  markup-stripped, over values **and** keys, failing closed with closed-set
  labels. The same screens run at the HTTP adapter and inside `PreProcessNode`, so
  the guarantee holds when the agent runs behind a gateway with no adapter of ours
  in front of it.
- **Credentials in the request**: a credential-shaped string in `input` would be
  returned back into `PreProcessNode`'s own result as `validated_input`, where the
  framework's output gate finds it and aborts the run with a traceback the caller
  cannot act on. Both the adapter and the node screen for it first, using the
  framework's own detector so the refusal set matches the block set exactly, and
  name the field rather than the value.
- **Output**: `config/config.yaml` `security.s3_gate_enabled: true`. The gate is a
  module-level `_security_gate_output()` called inside `PostProcessNode.execute()`
  — not a node instance method, since the node's own gate methods are final and
  reserved by the framework. See "Output boundary".
- **Audit**: every `execute()` calls `emit_trace_event(event, payload, state)`
  positionally, on a reachable path. `node_start` / `node_complete` are emitted by
  `BaseNode.__call__` and are not duplicated here.
- **Secrets**: none. No secret accessor is called and `requires.secrets` is `[]`.

> **Routing-injection defence:** routing destinations (queue/handler) are resolved
> ONLY from the configured taxonomy carried in `classification_context` (loaded by
> BuildContextNode). RouteResolveNode uses the proposed category id as a lookup
> key; unknown or injected ids fall back to the taxonomy default queue. A queue
> name is NEVER read from the free-text request or the classification rationale,
> and the output gate re-checks that every rendered destination is declared.

### Output boundary

The envelope builder resolves the caller-visible output as
`formatted_output or result`, **with no status check**. Three consequences shape
this design:

1. an error path that leaves `result` populated ships the un-gated answer inside
   the error envelope — so on a violation the gate clears every output-bearing
   field (`result`, `routing_report`, `routing_decision`,
   `classification_result`) through one function, with an inventory guard in the
   tests so a new field cannot quietly join them;
2. a **falsy** `formatted_output` re-opens that fallback — so the withheld-notice
   is non-empty;
3. partial deltas are *merged* into state, so omitting a key leaves the old value
   in place — the clearing therefore writes each key explicitly, and the tests
   assert presence **and** emptiness rather than falsiness.

Violation messages carry a closed-set label and never the matched value. No
`get_output` in this template uses an `or` fallback, at either graph level.

A declined request reaches `post_process` with no routing report to surface, so
it writes through the same clearing function for the same reason — every
output-bearing field blanked, `formatted_output` carrying the caller-facing
sentence, which is non-empty and therefore cannot re-open the fallback. It
differs from a violation only in `status`: `success`, because the caller can
correct the value and send the request again.

The one caller-supplied value that reaches the rendered document is `channel`,
and it is re-checked against the inert-identifier pattern at the point of
rendering — so the document cannot carry caller-written text even if state were
populated by something other than this adapter.

### Numeric handling

The rendered confidence goes through a finite, bounded parser. NaN and the
infinities survive `float()` and then compare False against every bound, so the
natural `not (x < low or x > high)` spelling of a range check admits them; the
explicit finiteness test is what makes it fail closed.

### Request metadata across the subgraph boundary

The boundary forwards only the request **string**: the inner graph builds a fresh
state, so nothing the outer nodes wrote is visible to the domain nodes. The outer
node stashes the metadata in a ContextVar immediately before delegating
(`src/graph/context_bridge.py`) and the inner graph seeds its initial state from
it. A ContextVar rather than a module global so concurrent requests in one
process cannot read each other's metadata.

### Composition Pattern

- **Pattern**: GraphNode (subgraph) — outer AgentBaseGraph → ServiceRoutingGraphNode → inner BaseGraph
- **Composition target**: DomainWorkflowGraph (src/graph/domain_workflow_graph.py)
- **Error propagation strategy**: propagate (fail-fast; inner errors surface as SubgraphError)
- **Declined request**: never enters the subgraph — `ServiceRoutingGraphNode.execute()`
  returns the settled reason unchanged rather than delegating, so the inner graph
  cannot replace a specific, actionable reason with its own first-node precondition
  failure. `merge_output()` prefers the outer reason for the same reason: reading
  `sub_result` alone would overwrite it with the blank of a graph that never ran

## Import Isolation Confirmation
- [x] Template does not import the platform SDK directly
- [x] Import targets: framework/ and shared/ only (+ intra-template src/)

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| Framework base type | AgentBaseGraph | AutonomousBaseGraph | AgentBaseGraph | Deterministic linear pipeline, no autonomous loop |
| Composition pattern | Standalone | GraphNode (subgraph) | GraphNode | Nested: outer backbone + inner domain graph |
| Routing source | request-derived | configured taxonomy | configured taxonomy | Routing-injection defence |
| Classifier | model-driven | deterministic keyword | deterministic keyword | Fully testable, side-effect-free, never fabricates a routing destination |
| Credential in the request | let it fail inside the graph | refuse at the boundary | refuse at the boundary | The request cannot succeed either way; a named refusal beats an opaque node-1 error. Terminal: a credential reaching the request is a containment event, not a formatting mistake to correct |
| Correctable value (empty / too short / too long / too few words) | terminate with `status=error` | complete with `status=success` + reason code | complete | Terminating ends the turn and surfaces only an exception type; completing names the one value to change and keeps the conversation open |
| Reason surfaced to the caller | `error_code` in the envelope | one fixed sentence | sentence | A code in the envelope is a second contract to hold in step with the first, and invites branching on an internal condition name |
| Fully-redacted request | classify the placeholders | refuse, naming the cause | refuse | Classifying nothing yields a confident decision from no content |
| Rendered `channel` | trust the entry-point check | re-check at render | re-check at render | The document must not carry caller text regardless of who populated state |

## Configurable routing taxonomy

The routing taxonomy — the allowed categories and their queues/handlers — is
declared in `config/config.yaml` under `routing`, so a customer can override
categories, queues and handlers for service-specific routing **without a code
change**. `src/config_loader.py` is the single reader; every node reaches it
through that module.

When the block is absent or malformed, the built-in default in
`src/config_loader.py` applies and the audit event records which of the two
resolved the request (`taxonomy_source: config | builtin_default`), so a
misconfigured deployment is visible in the trail rather than silently degraded.

Routing destinations exist ONLY in this table — never derived from the request —
and the output gate refuses any response whose destination is not declared in it.
