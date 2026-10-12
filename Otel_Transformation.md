# OTEL Transformation — Agent Template

This document records what was actually done to wire `cams_otel_lib` into this
agent per **OTEL Transformation Prompt v4** (`Otel_Transformation_Prompt_v4 2.md`).

It supersedes the previous version of this file, which described a `platform_sdk_common`-based
integration and referenced files (`observability.py`, `error_tracking.py`, `metrics.py`,
`rate_limiting.py`, `config_loader.py`, `secrets_loader.py`) that are not part of this
scaffold's currently-selected features. That version had drifted out of sync with the
shipped code — the code already imported `cams_otel_lib` directly, but with v2-era
`RequestContext` field names (`tenant_name`, `scope_name`, `userid`, `app_name`) and no
`Otel_Client` reuse, no structured `log_event` calls, and a stale `platform-sdk-common`
pin in `requirements.txt`. This revision brings the code and the doc back in sync.

---

## Target: Pro-code agent (§4a, Pattern A)

This is a dedicated-pod-per-agent template (`python -m src.agent`, one FastAPI app,
one `AgentExecutor`), so **§4a Pattern A — raw headers with config-file fallback** applies.
Pattern B (JWT/claims decoding) was not used because this template doesn't validate/decode
a bearer token upstream.

## Files changed

| File | What changed |
|---|---|
| `requirements.txt` | `cams-otel-lib` bumped to `>=0.2.0` (0.2.0 has the renamed `RequestContext` fields + `Logger.log_event`). Removed the stale, unused `platform-sdk-common==0.1.3` pin. Added `opentelemetry-instrumentation-fastapi==0.63b1` and `opentelemetry-instrumentation-requests==0.63b1` — `src/agent/__main__.py` already imported and used `FastAPIInstrumentor`/`RequestsInstrumentor` but the packages were never listed as dependencies. |
| `src/middleware/claims_middleware.py` | Rewritten. `RequestContext` kwargs renamed to the current schema (`tenant_id`, `app_id`, `user_id`, `agent_id`, `agent_instance_id`, `system_id`, `org_id`, `project_id`, `session_id`, `service_name`). `Otel_Client` now reused via `get_current_otel_client() or initialize_otel_client(...)` instead of unconditionally reinitializing every request. Added `system_id`/`org_id`/`project_id` header+config-fallback sourcing (previously only `tenant_id`/`user_id`/`app_name` were sourced — `system_id`/`org_id`/`project_id` default to `"N/A"` since this repo doesn't define an org/system/project hierarchy today). Added `AUTH_SUCCESS`/`AUTH_FAILURE` `log_event` calls around context setup. |
| `src/agent/nodes/agent_node.py` | Added `LLM_REQUEST`/`LLM_RESPONSE`/`LLM_ERROR`/`LLM_TOKEN_COUNT` `log_event` calls around the `AgentNode`'s `llm_with_tools.ainvoke(...)` call. Added `MCP_TOOL_INVOKE_ENTRY`/`MCP_TOOL_INVOKE_EXIT` `log_event` calls around each `tool.ainvoke(...)` call in `ToolNode.execute`. |
| `src/agent/executor.py` | Added `AGENT_MEMORY` (`MEMORY_READ` before, `MEMORY_WRITE` after) `log_event` calls around the checkpointed `compiled_graph.ainvoke(...)` call in `_run_graph` — this is the closest explicit call site to "checkpoint read/write", since `MemorySaver`'s own persistence happens inside LangGraph and isn't independently callable. |
| `src/agent/__main__.py` | Added `@otel_trace` to `_read_agent_config_field` (was missing it). |
| `src/config/agent_config.py` | Added `@otel_trace` to `_deep_merge` and the (currently unused/dead — `kubernetes_configmap` feature not selected in this build) `_merge_kubernetes_configmap`. |
| `src/config/cams_config_adapter.py` | Added `@otel_trace` to `CAMSConfigAdapter.__init__`. |
| `src/tools/registry.py` | Added `@otel_trace` to `ToolRegistry.__init__`. |
| `src/tools/mcp_loader.py` | Added `@otel_trace` to `load_tool_configs` and `load_mcp_tools` (both were missing it; the `search_fn` seen in this file is example code inside a docstring, not real code — nothing to annotate there). |
| `src/utils/health.py` | Added `@otel_trace` to `ComponentHealth.__init__`, `ComponentHealth.to_dict`, and `HealthChecker.__init__`. |

Every other function/method already imported from `cams_otel_lib` and carried `@otel_trace`
(`src/utils/llm_provider.py`, the rest of `src/utils/health.py`, `src/tools/__init__.py`,
`src/config/agent_config.py`'s public functions, `src/config/cams_config_adapter.py`'s
other methods).

## Identity field sourcing (this repo specifically)

| `RequestContext` field | Source |
|---|---|
| `request_id` | `X-Request-ID` header, else a generated UUID |
| `tenant_id` | `X-Tenant-ID` header, else `agent_config.json`'s `runtime_context.tenant_id` |
| `user_id` | `X-User-ID` header, else `runtime_context.user_id` |
| `app_id` | `X-App-Name` header, else `agent_definition.agent_id` |
| `agent_id` | `agent_definition.agent_id` — **same field as `app_id`** (see below) |
| `agent_instance_id` | `AGENT_INSTANCE_ID` env var, else `runtime_context.instance_id` |
| `session_id` | `X-Session-ID` header, else a generated UUID |
| `system_id` / `org_id` / `project_id` | corresponding headers, else `runtime_context.{system_id,org_id,project_id}` — all currently `"N/A"` since this config doesn't populate them |
| `service_name` | `settings.agent_name` |

**`agent_id` == `app_id` here on purpose.** Per v4 §2, for agents deployed through
`agent-registry`, Registry's `agent_id` column is populated from the Workbench `app_id` at
agent-creation time and carried forward across every version — so both are read from the
single `agent_definition.agent_id` field in `agent_config.json` rather than two
independently-sourced values. `Otel_Client`'s own `agent_id` parameter (a different concept —
it identifies the running pod/resource) is passed `agent_instance_id`, not
`agent_definition.agent_id`.

## Structured `log_event` categories wired

- **LLM** (`agent_node.py`, `AgentNode.execute`) — `LLM_REQUEST` before the LLM call,
  `LLM_RESPONSE` after, `LLM_ERROR` on exception, `LLM_TOKEN_COUNT` when `usage_metadata`
  is present on the response.
- **MCP** (`agent_node.py`, `ToolNode.execute`) — `MCP_TOOL_INVOKE_ENTRY` before each
  `tool.ainvoke(...)`, `MCP_TOOL_INVOKE_EXIT` after (success or failure, `log_level=ERROR`
  on failure).
- **AGENT_MEMORY** (`executor.py`, `_run_graph`) — `MEMORY_READ` before and `MEMORY_WRITE`
  after the checkpointed `compiled_graph.ainvoke(...)` call.
- **AUTHENTICATION** (`claims_middleware.py`, `dispatch`) — `AUTH_SUCCESS` when
  `RequestContext`/`Otel_Client` setup succeeds, `AUTH_FAILURE` on exception. Note: this
  middleware does not perform real authentication (no token validation) — these events mark
  identity-context extraction succeeding/failing, not a security decision.
- **AI_SERVICE** — deliberately left unwired. This template has no vision/OCR/TTS-style
  integration; there is no real call site to attach `AI_SERVICE_ENTRY`/`EXIT`/`ERROR` to.
  Add it if/when such an integration is added.
- **`src/tools/simple_test_tool.py`'s `@tool`-decorated functions** (`simple_calculator`,
  `text_counter`) were deliberately **not** annotated with `@otel_trace`, unlike other
  functions in the repo. Stacking `@otel_trace` under LangChain's `@tool` decorator risks
  interfering with the argument-schema introspection `@tool` performs on the function
  signature, and their invocations are already fully covered by the
  `MCP_TOOL_INVOKE_ENTRY`/`EXIT` events wrapping `tool.ainvoke(...)` in `ToolNode.execute`
  above — a trace span on the raw function body would be redundant with that.

## §8 — Log verification (OpenSearch)

Not yet verified against a live OpenSearch cluster from this environment (no deployed pod
available at the time of this change). To verify: run the agent with `OTEL_ENABLED=true`
(+ `OTEL_LOGGING_ENABLED`/`OTEL_REMOTE_LOGGING` as needed), send a request that reaches the
agent/tools loop, and confirm a record shaped like the v4 §8 example lands in `logs-otel-v1`
with `agent_instance_id`, `tenant_id`, `app_id`, `message_category`, `message_subtype`, and
`attributes.otel.trace_id`/`span_id` populated.

## §8b — Trace verification (OpenSearch)

**Not yet run against a live deployment** — this repo was only exercised locally in this
session (no Kubernetes pod, no OpenSearch/collector access from here). The instrumentation
required to produce spans is in place (`FastAPIInstrumentor`/`RequestsInstrumentor` wired in
`src/agent/__main__.py`'s `main()`, `@otel_trace` on the functions listed above), but §8b's
verification steps (query `ss4o_traces-otel-default`, confirm a multi-span trace with intact
`parentSpanId` chain, confirm `OTEL_TRACING_ENABLED`/`OTEL_REMOTE_TRACING` in the live pod
env) have not been completed and should be done as part of this agent's next real deployment.

Known gotchas to keep in mind when that verification happens (both confirmed, not
hypothetical — see v4 §8b):
- Traces land in `ss4o_traces-otel-default`, a **different** index from logs
  (`logs-otel-v1`) — don't look for them in the logs index.
- Every span's `@timestamp` field is a Go zero-value (`"0001-01-01T00:00:00Z"`) at the
  currently-deployed collector version — **sort/filter on `startTime`, not `@timestamp`**,
  including when setting up an OpenSearch index pattern's time field.
- A single HTTP request produces multiple span documents sharing one `traceId` (one root
  `kind: "Server"` span + several `kind: "Internal"` ASGI-lifecycle children) — that's
  expected, not duplication. Verification should confirm the `parentSpanId` chain resolves
  back to the root span within that `traceId`, not just that "a span exists somewhere."

## Environment variables required at deployment

| Variable | Purpose |
|---|---|
| `OTEL_ENABLED` | Master enable/disable switch |
| `OTEL_LOGGING_ENABLED` / `OTEL_REMOTE_LOGGING` | Enable log export |
| `OTEL_TRACING_ENABLED` / `OTEL_REMOTE_TRACING` | Enable trace export — required for §8b spans to appear at all |
| `OTEL_CONSOLE_LOGGING` / `OTEL_CONSOLE_TRACING` | Emit to stdout (useful for local debugging) |
| `OTEL_ENDPOINT` | Remote OTLP collector endpoint |
| `OTEL_CONFIG_UUID` | Stable UUID for OTEL config (read once at import time in `src/tools/__init__.py`) |
| `AGENT_INSTANCE_ID` | Set by `agent-registry` at deploy time for K8s/Cloud Run/Azure Functions targets; falls back to `agent_config.json`'s `runtime_context.instance_id` if unset |
| `ENVIRONMENT` / `ENV` | Deployment environment (`dev`/`staging`/`prod`) |

Keep this document in sync with the code going forward — update the tables above whenever
identity-field sourcing, wired `log_event` categories, or verification status change.

## 2026-08-24 — Drift audit and fix

Re-ran `scripts/audit.py` against this repo (drift check, not a fresh migration — identity
mapping above was already correct and untouched). Found and fixed 2 errors:

| Finding | File | Fix |
|---|---|---|
| `client_reuse` | `src/tools/__init__.py` (module-level `initialize_otel_client` call) | Was unguarded, and sourced `service_name` from `os.getenv("AGENT_NAME", "agent")` while `src/agent/__main__.py`'s call used `settings.agent_name` (default `"LangGraph Agent"`) — a real mismatch that derives a different `otel_config_uuid` per call site and permanently breaks trace export for the process (per the invariant's mechanism: `trace.set_tracer_provider()` only succeeds once). Changed to `Otel_Client.get_current_otel_client() or Otel_Client.initialize_otel_client(...)` and switched to `settings.agent_name` for consistency with the other call site. |
| `client_reuse` | `src/agent/__main__.py` (`main()`) | Same unconditional-call issue; guarded with `get_current_otel_client() or initialize_otel_client(...)`. |
| `context_reset` | `src/middleware/claims_middleware.py` (`OtelContextMiddleware.dispatch`) | `reset_request_context`/`reset_observability_client` ran after `call_next` on the straight-line path only — an exception raised inside `call_next` skipped both resets, leaking that request's context into whichever request the worker picked up next. Wrapped `await call_next(request)` in `try/finally` so both resets always run. No other control flow changed. |

Left unwired, by choice: the `stdlib_logger` warning on `src/utils/structured_logging.py`
(`logging.getLogger`-based, no OTEL schema fields). Nothing else in the repo imports this
module — confirmed dead code — but deleting it is a functional change outside instrumentation
scope, so it was left in place per user decision.

Post-fix audit: `0 error(s), 1 warning(s), 3 info.` (the warning above). All three edited
files parse cleanly; not re-verified against a live OpenSearch/collector in this pass — see
§8/§8b above, still outstanding from the original migration.
