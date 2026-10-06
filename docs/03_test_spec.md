# Test Specification — SVC-C2-018 Service Request Classification & Routing Agent

## 1. Test Strategy

- **Agent:** SVC-C2-018 — Service Request Classification & Routing Agent (Cat 2,
  Chat pattern, two-layer nested graph: outer `AgentBaseGraph` backbone + inner
  `DomainWorkflowGraph` `BaseGraph`).
- **Input:** FREE TEXT service request (a service ticket / complaint / HR ask) —
  **not** structured data. Classification and taxonomy routing happen inside the
  inner domain workflow.
- **Coverage target:** ≥ 90% of `src/nodes/` + `src/graph/` branches.
- **Test types:** Unit (per node + graph wiring) · Proof-of-Boundary (framework
  security/serialization contracts) · Backbone invoke · **served-request tests
  that drive the real ASGI `/invoke`**.
- **Framework provisioning:** `framework` (agenticstar-agentcore) is supplied by
  CI as the wheel from the package registry. Tests import the real modules; there
  are no stub nodes.
- **State:** all shared list/dict state fields are JSON strings, built via
  `to_json(...)` and read via `from_json(...)`.
- **Audit:** `emit_trace_event` is patched at the node module level in unit tests
  to avoid audit-backend calls, never via a `sys.modules` stub (which would break
  the real `shared` package the framework loads at import time).

> **Why the served-request suite exists.** Every other module here calls a node
> or a graph directly, and the two surfaces can disagree: a pipeline whose unit
> suite is entirely green still serves nothing if the entry point never
> establishes a trust level the first node accepts, and a refusal that is correct
> inside the graph can reach the caller as an unexplained error. The served
> suite imports the application and drives `POST /invoke`.

### Test file map

| File | Scope |
|------|-------|
| `tests/unit/test_nodes.py` | All 7 domain/backbone nodes + outer & inner graph wiring; the node-owned screens via direct `execute()` |
| `tests/unit/test_screens.py` | Caller-input screens — hostile **and** benign, control tokens, keys, credential-detector parity, finite parser, privacy-filter guard |
| `tests/unit/test_output_gate.py` | Output-gate containment: cleared set, truthy notice, taxonomy invariant, clean-path control |
| `tests/unit/test_runtime_config.py` | `config/config.yaml` is read, and a declared value changes behaviour end to end |
| `tests/unit/test_framework_compliance_tc06_tc07.py` | TC-06/TC-07 — the framework's node gates are non-overridable |
| `tests/proof_of_boundary/test_server_invoke.py` | Served requests through the real ASGI `/invoke`: routing, trust, caller context, boundary refusals, error-envelope shape |
| `tests/proof_of_boundary/test_pb_invoke_order.py` | PB-6 per-node + backbone invoke order (VERIFIED_EXTERNAL) + node-level trust gate + payload alignment |
| `tests/proof_of_boundary/test_import_isolation.py` | PB-4 platform-SDK import isolation (AST scan) |
| `tests/proof_of_boundary/test_state_safety.py` | PB-2/PB-5 State msgpack/credential safety (AST scan) |
| `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` | PB-7 HITL interrupt-propagation (skip stub — no cross-boundary HITL) |

### Canonical valid payload (PB-6 `_VALID_PAYLOAD`)

The free-text IT-support request used by the backbone invoke test and by
`deploy/invoke_payload.json` (the two MUST stay identical — asserted by
`test_invoke_payload_matches_pb6`):

```
My work laptop cannot connect to the corporate VPN and I am blocked from
accessing my email. Please reset my network access as soon as possible.
```

Routing outcome: the deterministic classifier matches the `it_support` signals
(laptop / vpn / email / network / access / reset) and `RouteResolveNode` resolves
that category strictly from the configured taxonomy to `itsm_queue` / `IT Service
Desk` ⇒ success end-to-end. The request clears both the outer `PreProcessNode`
boundary (non-empty, 3..20000 chars) and the inner `InputValidateNode` domain
gate (≥ 2 words).

## 2. Framework Compliance Tests (Mandatory)

| TC-ID | Test | Expected Result | Where |
|-------|------|----------------|-------|
| TC-01 | State contract: flat `TypedDict`, domain fields `NotRequired`, no Pydantic/dataclass | AST scan: 0 violations | `test_state_safety.py` |
| TC-02 | Empty / too-short / too-long request declined at PreProcessNode | the run **completes**: `status=success` with an `error_code` set; error_log names the reason | `TestPreProcessNode` |
| TC-02a | Control token / spliced directive / credential / fully-redacted request refused at PreProcessNode | the run **terminates**: `status=error`; error_log names the screen, never the value; `validated_input` absent | `TestPreProcessNode` |
| TC-03 | No JWT/credential in State | CI `gate-credential-scan`: 0 violations | CI + `test_state_safety.py` |
| TC-04 | `execute(self, state)` contract — no `_invoke_impl` | Signature `(self, state)`, `_invoke_impl` absent | `test_execute_signature_is_state_first` |
| TC-05 | `emit_trace_event()` called inside each node `execute()` | ≥1 domain event per node (positional form) | `scripts/check_audit_trace.py` (CI gate) |
| TC-06 | The framework's node input gate cannot be overridden | class definition raises `TypeError` | `test_framework_compliance_tc06_tc07.py` |
| TC-07 | The framework's node output gate cannot be overridden | class definition raises `TypeError` | `test_framework_compliance_tc06_tc07.py` |
| TC-08 | `required_trust_level` enforced in `__call__` before `execute()` — asserted at NODE level | ANONYMOUS caller → refused; VERIFIED_EXTERNAL → admitted | `TestTrustGate` |
| TC-08a | Outer `PreProcessNode` = VERIFIED_EXTERNAL; inner nodes + post_process = ANONYMOUS | trust levels asserted per node | `test_trust_level_*` |
| TC-11 | Output gate on post_process | credential pattern or undeclared destination → withheld + `status=error`; clean → pass | `test_output_gate.py` |
| TC-12 | No unreachable security layer on the agent class | `AgentBaseGraph` exposes no output-gate hook; the class defines none | `test_agent_declares_no_unreachable_gate_method` |

> **Trust is asserted at the node level, not the graph level.** The backbone
> `pre_process → main` edge is unconditional, so a graph-level ANONYMOUS invoke can
> still reach a terminal state; the real contract lives on each node's
> `required_trust_level`, exercised directly via `PreProcessNode.__call__`.

## 3. Proof-of-Boundary Tests (Mandatory)

| PB-ID | Boundary | Test | Expected Result | Where |
|-------|----------|------|----------------|-------|
| PB-2 | State serialization | AST scan of `src/schemas/state.py` | primitives only; no Pydantic/dataclass | `test_state_safety.py` |
| PB-4 | Import isolation | AST scan of `src/` | 0 direct platform-SDK imports | `test_import_isolation.py` |
| PB-5 | Checkpoint safety | no credential-named fields / prohibited types in State | inspection pass | `test_state_safety.py` |
| PB-6 | Invoke execution order (per node) | `__call__`: node_start → trust gate → input gate → `execute()` → output gate → node_complete | order verified for every `src/nodes/` class | `TestInvokeOrder` |
| PB-6b | Backbone invoke order | full `Graph().invoke(_VALID_PAYLOAD, ctx=VERIFIED_EXTERNAL)` | `status=success`; node_history = `[Initialize, PreProcess, ServiceRoutingGraphNode, PostProcess, Finalize]` | `TestBackboneInvokeOrder` |
| PB-6c | Real external caller | `InvocationContext(caller_trust_level=VERIFIED_EXTERNAL)` — **never** `for_internal()` | inner ANONYMOUS nodes accept the passthrough trust; SUCCESS end-to-end | `TestBackboneInvokeOrder` |
| PB-6d | Node-level trust gate | `PreProcessNode.__call__` with ANONYMOUS vs VERIFIED_EXTERNAL caller | ANONYMOUS → trust-gate denial; VERIFIED_EXTERNAL → admitted + SUCCESS | `TestTrustGate` |
| PB-6e | Payload alignment | `deploy/invoke_payload.json["input"] == _VALID_PAYLOAD` | the deployment evidence invoke exercises the PB-6 payload | `test_invoke_payload_matches_pb6` |
| PB-7 | HITL interrupt propagation | skip stub — `propagate_hitl=False`, no cross-boundary interrupt() checkpoint | skipped with reason (real assertion when HITL wired) | `test_pb7_hitl_interrupt_propagation.py` |
| PB-8 | Served request | `POST /invoke` with a bearer token | `status=success`, real routing document, expected `node_history` | `TestServedRequest` |
| PB-8a | Output depends on input | five different requests through `/invoke` | five different categories and queues | `test_the_answer_depends_on_the_request` |
| PB-8b | Caller trust at the entry point | no token / wrong token | 401, generic body that does not say which | `TestCallerTrust` |
| PB-8c | Caller context contract | `channel` declared / free-text / undeclared key | accepted and rendered · 400 naming the field · dropped, request still succeeds | `TestCallerContext` |
| PB-8d | Boundary refusals | control tokens, spliced directive, credential, fully-redacted request, oversize | 400 with a closed-set reason; benign look-alikes still route; an empty request instead **completes** with `status=success` and the empty-input sentence | `TestBoundaryRefusals` |
| PB-8e | Envelope for a declined request | a request too short to classify | `status=success`; `output` is the fixed sentence for that reason; envelope keys are `output`/`status`/`trace_id`/`correlation_id`/`node_history` only — no `error_code`, no `error_log`, no released text, no traceback, no source paths | `TestErrorEnvelopeCarriesNothing` |
| PB-9 | Declared config reaches behaviour | change a queue in `config/config.yaml` | the changed queue is rendered by `/invoke` | `TestDeclaredValueChangesBehaviourEndToEnd` |

## 4. Business Logic Tests

| BL-ID | Test | Input | Expected Result | Where |
|-------|------|-------|----------------|-------|
| BL-01 | Happy-path classify + route | `_VALID_PAYLOAD` | routing report with `SERVICE REQUEST ROUTING DECISION` + `itsm_queue`; SUCCESS | `test_backbone_invoke_succeeds_and_returns_output`, `TestInnerDomainGraph` |
| BL-02 | Request normalisation | `"  reset   my    VPN  access  "` | whitespace collapsed → `"reset my VPN access"` | `test_whitespace_is_normalised` |
| BL-03 | IT-support classification | password/laptop/vpn text | `category=it_support`, matched signals non-empty | `test_it_support_classification` |
| BL-04 | HR / complaint classification | leave/payroll · complaint/refund text | `hr_request` · `customer_complaint` | `test_hr_request_classification`, `test_customer_complaint_classification` |
| BL-05 | Default (no signal) | unroutable text | `category=general_inquiry`, confidence 0.3, no signals | `test_no_signal_defaults_to_general_inquiry` |
| BL-06 | Urgency escalation | "server is down … urgent" | priority escalated to `critical` | `test_urgency_escalates_priority_to_critical` |
| BL-07 | Configuration-only routing | classification → routing | queue/handler resolved from the configured taxonomy; `routed_from_config=True` | `TestRouteResolveNode` |
| BL-08 | **Routing-injection defence** | injected/unknown category id | falls back to the default queue; the injected string is never used as a queue | `test_unknown_or_injected_category_falls_back_to_default` |
| BL-09 | Report assembly | routing_decision + classification | full routing report (channel / category / queue / handler / priority / confidence / fallback / signals) | `TestOutputFormatNode` |
| BL-10 | Graph key coupling | inner `get_output` ↔ outer `merge_output` | 4 coupled keys mapped; `merge_output` returns changed keys only | `TestOuterGraphComposition`, `TestInnerDomainGraph` |
| BL-11 | Caller channel crosses the subgraph boundary | `input_context.channel` | the value is rendered in the document; absent → `unknown` | `test_the_caller_channel_reaches_the_rendered_document` |
| BL-12 | Confidence is finite and bounded | NaN / ±Infinity / out-of-range | rendered `n/a`, never `nan%` | `TestFiniteInRange`, `TestOutputFormatNode` |

### Negative / boundary cases

Two outcomes, not one. A value the caller can correct makes the run **complete**
carrying a reason code, so the caller can fix it and send the request again; a
refusal the caller cannot reword their way past **terminates** with
`status=error`. The Expected column says which, for each case.

| Case | Node | Expected |
|------|------|----------|
| empty / whitespace `user_input` | PreProcessNode | completes: `status=success`, `error_code` set, "empty" |
| request < 3 chars | PreProcessNode | completes: `status=success`, `error_code` set, "too short" |
| request > 20,000 chars | PreProcessNode | completes: `status=success`, `error_code` set, "exceeds" |
| empty request text | InputValidateNode | completes: `status=success`, `error_code` set, "empty" |
| single-word request | InputValidateNode | completes: `status=success`, `error_code` set, "too few words" |
| missing `service_request` | BuildContextNode | terminates: `status=error` |
| missing `classification_context` | GenerateResponseNode | terminates: `status=error` |
| missing `classification_result` | RouteResolveNode | terminates: `status=error` |
| empty routing taxonomy | RouteResolveNode | terminates: `status=error`, "taxonomy" |
| unknown/injected category | RouteResolveNode | default-queue fallback, `fallback=True` |
| missing `routing_decision` | OutputFormatNode | terminates: `status=error` |
| empty `routing_report` | PostProcessNode | fallback message, `status=success` |
| credential shape in the response | PostProcessNode | terminates: withheld, every output-bearing field cleared, `status=error` |
| destination not in the taxonomy | PostProcessNode | terminates: withheld, every output-bearing field cleared, `status=error` |
| control token / spliced directive in the request | adapter + PreProcessNode | terminates: 400 at the adapter; `status=error` at the node, closed-set label, value never echoed |
| credential in the request | adapter + PreProcessNode | terminates: 400 at the adapter; `status=error` at the node, field named, value never echoed |
| request emptied by the privacy filter | adapter + PreProcessNode | terminates: 400 at the adapter; `status=error` at the node, naming that cause |
| free-text `input_context.channel` | adapter | 400 naming the field |
| undeclared `input_context` key | adapter | dropped; the request still succeeds |

The missing-upstream-key rows are invariant guards, not caller mistakes:
the node that needed the key cannot make one up, and there is nothing for a
caller to correct, so they terminate like the screens above them.

The node-level cases assert that a code is set and that `error_log` names the
reason. Which code maps to which caller-facing sentence is pinned end to end
instead: a whitespace-only request returns the empty-input sentence and a
request too short to classify returns the invalid-value sentence, both asserted
against the constants in `src/services/failure_message.py`
(`test_an_empty_request_produces_no_routing_decision`,
`test_the_error_envelope_is_bare`). The `error_code` itself never appears in the
envelope — the reason reaches the caller as the sentence and nothing else.

### Both directions

Every screen is tested for what it must **not** refuse as well as what it must.
The benign counterparts are drawn from real service-desk language — "Please
ignore the previous ticket I raised", "The system prompt on the login page says
my password expired" — because a screen that refuses those blocks the work this
agent exists to do, and that is the failure a service desk actually notices.

### Verifying that the tests are load-bearing

Each guard was reverted individually and the suite re-run; the original shipped
`src/` was then restored and the suite re-run against it. Layered guards mask one
another, so a guard that can be removed with the suite still green is not being
tested by it. Two guards initially showed exactly that and gained tests until
they did not: the node-owned content screen (masked by the adapter screen — now
covered by direct `execute()` cases) and the request-metadata bridge (nothing
observed the channel — now rendered and asserted end to end).

Restoring the original `src/` produces collection **errors**, not failures, for
the modules that import symbols the current code introduced; the suite is
re-run with those modules excluded so the summary line reflects real failures.

## 5. Test Execution Summary

- Execution: `pytest tests/` against the framework wheel.
- Total: unit + Proof-of-Boundary suites — all passing, **1 skipped** (PB-7 skip
  stub, by design).
- Gates: `gate-dep-pinning`, `gate-stub-check`, `gate-cat-consistency`,
  `gate-audit-trace-check`, `gate-manifest-schema`, `gate-oss-license`,
  `gate-forbidden-strings`, `gate-credential-scan`, import-isolation,
  composition, invoke-chain, trust-level, scaffold-integrity.
- Coverage: node + graph modules exercised on both success and error paths, plus
  the served request path through the real ASGI application.
