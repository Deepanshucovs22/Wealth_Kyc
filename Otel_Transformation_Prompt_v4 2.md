You are a super coder who can readjust code with the given instructions. This task is called OTEL Transformation (v4).

This prompt supersedes `Otel_Transformation_Prompt_v3.md`. v3 finalized the mandatory log schema (categorized structured logging, renamed `RequestContext` fields). v4 originally made no changes to any of that beyond adding a **trace verification section v3 never had** (§8b): v3 told integrators how to produce traces (`@otel_trace`, FastAPI/requests auto-instrumentation) but only ever gave a "what to verify against" JSON shape for *logs*, never for traces — so integrators had no way to confirm tracing actually worked end-to-end, and no warning about a real, confirmed OpenSearch-side quirk that makes correctly-flowing traces look broken if you don't know about it.

**Since then, a second update landed in this same v4** (no new version bump — this is still v4): a registry-side investigation (`OTEL_FIELD_FLOW_PLAN.md` at the repo root of the CAMS platform monorepo) confirmed that for agents deployed through `agent-registry`, `app_id` and `agent_id` are **the same underlying value** — Registry's `agent_id` column is populated from the Workbench `app_id` at agent-creation time and carried forward across every version. §2 and §4a below now say this explicitly, and §4a/§4b's `agent_instance_id` sourcing guidance is tightened from "wherever your config defines the agent" to concrete, confirmed sources now that Registry actually populates them. If you already ran v3/the original v4, you don't need to touch any code for the trace-verification half (§8b) — but the `agent_id`/`app_id` unification in §2/§4a/§4b is a real, previously-missing piece of guidance worth re-checking your integration against, particularly if your `claims_middleware.py`-equivalent currently reads `app_id` and `agent_id` from two independently-sourced fields.

The library is `cams_otel_lib` (pin `cams-otel-lib==0.2.0` or later in `requirements.txt` — 0.2.0 renamed several `RequestContext` fields to match the mandatory schema and added `Logger.log_event(...)` plus the category/subtype/log-level enums this prompt relies on).

---

## 0. Correct imports and API — do not use old names from v2 or earlier

- Import from `cams_otel_lib` directly (**not** `platform_sdk_common`/`platform_sdk.common.*` — stale, no longer exists):
  ```python
  from cams_otel_lib import (
      Logger as logger,
      Otel_Client,
      otel_trace,
      RequestContext,
      MessageCategory,
      LogLevel,
      MCPSubtype, LLMSubtype, AIServiceSubtype, AgentMemorySubtype, AuthenticationSubtype,
      set_request_context,
      set_observability_client,
      reset_request_context,
      reset_observability_client,
  )
  ```
- `RequestContext(...)` keyword arguments, **renamed from v2** to match the mandatory schema field-for-field:

  | v2 name (do not use) | Current name |
  |---|---|
  | `tenant_name` | `tenant_id` |
  | `scope_name` | `scope` |
  | `app_name` | `app_id` |
  | `userid` | `user_id` |
  | `instance_id` | `agent_instance_id` |

  Full current signature: `request_id`, `tenant_id`, `app_id`, `user_id`, `scope`, `session_id`, `agent_id`, `agent_instance_id`, `system_id`, `org_id`, `project_id`, `retry_count`, `agent_harness_id`, `message_subtype`, `chat_conversation_id`, `correlation_id`, `parent_request_id`, `knowledge_base_name`, `service_name`. Passing the old v2 names will raise a `TypeError`.

## 1. Replace all loggers with the platform SDK Logger

Find every file with `import logging` + `logger = logging.getLogger(__name__)`, or any project-local logger factory (e.g. `get_logger(__name__)` from a local logging util). Replace both lines with:
```python
from cams_otel_lib import Logger as logger, otel_trace
```
Don't delete the old logging module outright if something else in the repo still depends on its file-rotation/formatting behavior independent of OTEL — check for other importers first.

## 2. `RequestContext` — identity/scope fields (per request, set once)

```python
RequestContext(
    request_id=request_id,
    tenant_id=tenant_id,
    app_id=app_id,               # kebab-case, e.g. "account-opening"
    user_id=user_id,
    session_id=session_id,
    agent_id=agent_id,                    # the agent's OWN identity (e.g. agent_definition.agent_id) — NOT the instance
    agent_instance_id=agent_instance_id,  # the specific running deployment/instance of that agent
    system_id=system_id,          # organizational scope, level 1
    org_id=org_id,                 # organizational scope, level 2 (org unit)
    project_id=project_id,         # organizational scope, level 3
    # scope is auto-computed as "system.org.project" from the three ids above if you
    # don't pass it explicitly — you usually don't need to pass it yourself.
    service_name=service_name,
)
```
**Important — `agent_id` vs `agent_instance_id`:** these must be two distinct values. If your repo currently has code like `agent_id=os.getenv("AGENT_ID")` where that env var actually holds an *instance* id, keep that value going into `agent_instance_id` instead, and source a real agent-identity value for `agent_id` from wherever your config defines the agent itself (e.g. `agent_definition.agent_id` in a CAMS-schema config file). If no such source exists yet, leave `agent_id="N/A"` (it auto-generates a UUID) rather than reusing the instance value for both.

**`agent_id` and `app_id` are usually the same value — don't source them independently.** For agents deployed through `agent-registry`, `agent_definition.agent_id` in your mounted config *is* the Workbench `app_id` (Registry populates it from there at agent-creation time and carries it forward across every version of that agent) — so `app_id=` and `agent_id=` should both read from that one field, not two separately-read ones. Concretely: if your config exposes `agent_definition.agent_id`, pass that same value into both kwargs; don't also go looking for a second, independent `app_id` source (e.g. a different header or a different config key) unless you've confirmed your specific deployment target actually keeps the two distinct.

If `system_id`/`org_id`/`project_id` genuinely don't apply to your service, leave them at `"N/A"` — don't invent placeholder values.

**Optional fields** — only set what you actually have available; unset ones are automatically excluded from every log line rather than showing up as clutter: `agent_harness_id`, `chat_conversation_id`, `correlation_id`, `parent_request_id`, `knowledge_base_name` (e.g. for RAG/KB-lookup logging).

## 3. `Otel_Client` initialization — reuse, don't reinitialize, per request

Do **not** call `Otel_Client.initialize_otel_client(...)` unconditionally on every request — that rebuilds the env-var-derived config from scratch each time. Reuse the existing client if one's already running:
```python
otel_client = Otel_Client.get_current_otel_client() or Otel_Client.initialize_otel_client(
    service_name=service_name,
    environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
    agent_id=agent_instance_id,  # Otel_Client's own "agent_id" param identifies the running resource/pod — pass the instance id here, not the agent-identity value
)
otel_client_token = set_observability_client(otel_client)
```
`RequestContext` itself should still be constructed fresh on every request/invocation (it's cheap, per-request data) — only the heavier `Otel_Client` provider/exporter object benefits from reuse.

## 4. Which target are you integrating?

### 4a. Pro-code agents (dedicated pod per agent, e.g. built from a CAMS agent template)

Create or update `src/middleware/claims_middleware.py` (or wherever your HTTP middleware lives) as a `BaseHTTPMiddleware`. Pick ONE of these two context-population patterns based on what your repo already has — don't force the other one in:

**Pattern A — raw headers with config-file fallback** (matches the actual shipped code in current pro-code agents today):
```python
request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex.lower())
tenant_id = request.headers.get("X-Tenant-ID") or _CONFIG_TENANT_ID or "N/A"
user_id = request.headers.get("X-User-ID") or _CONFIG_USER_ID or "N/A"
app_id = request.headers.get("X-App-Name") or _CONFIG_APP_NAME or "N/A"
session_id = request.headers.get("X-Session-ID", uuid.uuid4().hex.lower())
system_id = request.headers.get("X-System-ID") or _CONFIG_SYSTEM_ID or "N/A"
org_id = request.headers.get("X-Org-ID") or _CONFIG_ORG_ID or "N/A"
project_id = request.headers.get("X-Project-ID") or _CONFIG_PROJECT_ID or "N/A"
```
`_CONFIG_*` constants are module-level, read once at import time from your config file's fallback fields (mirror however `_CONFIG_TENANT_ID` etc. are already read in your repo, typically from a `runtime_context` block — e.g. `runtime_context.system_id`). Also add a `_CONFIG_AGENT_ID` constant sourced from your config's `agent_definition.agent_id` (a *different* section than `runtime_context` — don't conflate it with the instance id). For agents deployed through `agent-registry` this field is populated from Registry's own `agent_id` column, which is the same value as `app_id` — see the note in §2 — so it's fine (expected, even) for `_CONFIG_AGENT_ID` and whatever you're already using for `app_id`/`_CONFIG_APP_NAME` to end up equal; that's not a bug to chase down.

Your instance-id constant (`_AGENT_INSTANCE_ID` above) should check an `AGENT_INSTANCE_ID` env var first, falling back to the config-file read, the same env-var-plus-config-file resilience pattern already used for `tenant_id`/`app_name`:
```python
_AGENT_INSTANCE_ID = os.getenv("AGENT_INSTANCE_ID") or _read_config_field("instance_id")
```
Registry now sets this env var directly at deploy time for K8s/Cloud Run/Azure Functions targets (alongside the existing `AGENT_ID`/`DEPLOYMENT_ID` env vars), so it's usually present without needing the config-file fallback at all — but keep the fallback for older deployments or targets that don't inject it.

**Pattern B — decode an incoming JWT/claims token** (use this if your repo already validates and decodes a bearer token upstream):
```python
request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex.lower())
tenant_id = claims.get("tenant_uuid", "N/A")
user_id = claims.get("user_uuid", "N/A")
app_id = claims.get("app_name", "N/A")
session_id = claims.get("session_id", uuid.uuid4().hex.lower())
system_id = claims.get("system_id", "N/A")
org_id = claims.get("org_id", "N/A")
project_id = claims.get("project_id", "N/A")
```

Either way, wire it up in `dispatch()`:
```python
async def dispatch(self, request: Request, call_next):
    request_context_token = None
    otel_client_token = None
    try:
        # ... populate the variables above via Pattern A or B ...
        request_context = RequestContext(
            request_id=request_id, tenant_id=tenant_id, app_id=app_id,
            user_id=user_id, session_id=session_id,
            agent_id=_CONFIG_AGENT_ID, agent_instance_id=_AGENT_INSTANCE_ID,
            system_id=system_id, org_id=org_id, project_id=project_id,
            service_name=settings.agent_name,
        )
        request_context_token = set_request_context(request_context)

        otel_client = Otel_Client.get_current_otel_client() or Otel_Client.initialize_otel_client(
            service_name=settings.agent_name,
            environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
            agent_id=_AGENT_INSTANCE_ID,
        )
        otel_client_token = set_observability_client(otel_client)
    except Exception as e:
        logger.error(f"Error setting up OTEL request context: {e}")

    response = await call_next(request)

    if request_context_token:
        reset_request_context(request_context_token)
    if otel_client_token:
        reset_observability_client(otel_client_token)

    return response
```
Register it: `app.add_middleware(YourOtelMiddleware)` after CORS middleware in your app factory.

### 4b. No-code / shared-runtime services (one process serves many tenants/configs/agents concurrently)

Same middleware shape as 4a, but be deliberate about `agent_instance_id`: since a single process isn't "one agent's pod," it should map to whatever concept identifies *this specific deployment* within your system (e.g. a deployment id or config id already threaded through your request handling) — not the process itself, and not a copy of `agent_id`. There's no universal default here; map it explicitly to your own domain model. `agent_id` should map to the specific agent/config identity being served by the current request (usually already available from whatever config object your request already resolves) — for a CAMS shared runtime, that's the per-request agent config's own `agent_id` field (e.g. `camsA2ACard.agent_id`), which Registry now guarantees is the agent's *stable* identity (the same value across every version of that agent), not a per-version-row id. If your shared runtime's config resolution currently derives `agent_id` from something version-specific, switch it to read the stable field instead.

Wire the middleware into every FastAPI app factory your service exposes (a shared runtime may have more than one app entrypoint, e.g. a deploy-mode app and a playground/debug-mode app — both need it, independently).

### 4c. MCP servers

MCP servers often don't run on a standard per-HTTP-request ASGI cycle — many use stdio or SSE transport, and a "request" is really a tool invocation, not always a 1:1 HTTP call.
- Populate `RequestContext` at the **tool-call handler boundary** — wherever your server dispatches an incoming tool invocation — rather than via HTTP middleware. Source identity/scope from whatever metadata the calling agent already attaches to the tool call (custom headers if you're on an SSE/HTTP transport, or fields in the tool-call payload itself if you're on stdio transport).
- Still wrap each tool invocation with `set_request_context(...)` / `reset_request_context(...)` in a try/finally, so context doesn't leak across concurrent tool calls on a long-lived server process.
- Use the same `Otel_Client.get_current_otel_client() or ...` reuse pattern from section 3 — don't reinitialize per tool call.
- Wrap each tool invocation with `MCP_TOOL_INVOKE_ENTRY`/`MCP_TOOL_INVOKE_EXIT` events (see section 6).
- Annotate tool-handling functions with `@otel_trace` for span coverage.

## 5. Annotate functions with `@otel_trace`

Add `@otel_trace` (import: `from cams_otel_lib import otel_trace`) to every function and method in every `.py` file across the project, **except**:
- The lifespan/startup function where `Otel_Client` is first initialized at process boot.
- Decorator factories and `@contextmanager`-decorated functions.
- `@property` methods.
- Python generator/yielding functions.

## 6. Structured category logging — `Logger.log_event(...)`

This is new. `Logger.info`/`warning`/`error`/`debug` remain for generic, unstructured logging (unchanged) — but the mandatory schema's `message_category`/`message_subtype`/`retry_count`/`attributes` vary **per individual event**, not per request (an LLM call and a memory write within the same request are two separate events with different categories, sharing the same `RequestContext` identity fields). Use `Logger.log_event(...)` for these:

```python
Logger.log_event(
    message_category=MessageCategory.LLM,       # MCP | LLM | AI_SERVICE | AGENT_MEMORY | AUTHENTICATION
    message="LLM request sent to gpt-4",
    message_subtype=LLMSubtype.LLM_REQUEST,
    log_level=LogLevel.INFO,                    # TRACE | DEBUG | INFO | WARN | ERROR | FATAL
    retry_count=None,                           # omit to use the current RequestContext's retry_count (default 1)
    attributes={"model": "gpt-4", "temperature": 0.7},  # category-specific extras; otel.trace_id/span_id/parent_span_id auto-injected
)
```
Plain strings also work if you don't want to import the enums (`message_category="LLM"`).

**Where to call this, mapped to the real call-site patterns already found in pro-code agents:**
- **MCP** (`MCPSubtype.MCP_TOOL_LIST`, `MCP_TOOL_INVOKE_ENTRY`, `MCP_TOOL_INVOKE_EXIT`, `MCP_PROMPT_OPERATION`, `MCP_RESOURCE_ACCESS`): wrap `tool.ainvoke(...)` calls — log `MCP_TOOL_INVOKE_ENTRY` immediately before the call with the tool name/input in `attributes`, `MCP_TOOL_INVOKE_EXIT` immediately after with the result (or `LLM_ERROR`-equivalent handling on exception).
- **LLM** (`LLMSubtype.LLM_REQUEST`, `LLM_RESPONSE`, `LLM_ERROR`, `LLM_TOKEN_COUNT`, `LLM_CACHE_HIT`): wrap `llm.ainvoke(...)` calls — `LLM_REQUEST` before, `LLM_RESPONSE` after (or `LLM_ERROR` on exception), `LLM_TOKEN_COUNT` if usage data is available from the response.
- **AGENT_MEMORY** (`AgentMemorySubtype.MEMORY_READ`, `MEMORY_WRITE`, `MEMORY_UPDATE`, `MEMORY_DELETE`, `MEMORY_CLEAR`): wrap checkpoint/state read-write operations (e.g. `MemorySaver` checkpointing, explicit state dict reads/writes).
- **AUTHENTICATION** (`AuthenticationSubtype.AUTH_SUCCESS`, `AUTH_FAILURE`, `TOKEN_VALIDATION_SUCCESS`, `TOKEN_VALIDATION_FAILURE`, `PERMISSION_CHECK_SUCCESS`, `PERMISSION_CHECK_FAILURE`): wrap your claims/auth middleware's context-setting — log `AUTH_SUCCESS` when identity is successfully extracted, `AUTH_FAILURE` if it isn't.
- **AI_SERVICE** (`AIServiceSubtype.AI_SERVICE_ENTRY`, `AI_SERVICE_EXIT`, `AI_SERVICE_ERROR`, `AI_SERVICE_CLASSIFICATION`, `AI_SERVICE_EXTRACTION`): if your repo doesn't have a real vision/OCR/TTS-style integration yet, there's nothing to wire this into today — the category/subtype enums are ready for when you add one; don't fabricate a call site.

**Retry count**: defaults to 1 on `RequestContext`. If an operation is retryable, bump it before logging the retry attempt:
```python
for attempt in range(1, max_attempts + 1):
    get_request_context().retry_count = attempt
    try:
        result = await llm.ainvoke(messages)
        Logger.log_event(message_category=MessageCategory.LLM, message_subtype=LLMSubtype.LLM_RESPONSE, message="...")
        break
    except Exception as exc:
        Logger.log_event(message_category=MessageCategory.LLM, message_subtype=LLMSubtype.LLM_ERROR, message=str(exc), log_level=LogLevel.ERROR)
```

## 7. FastAPI/requests auto-instrumentation (HTTP-serving repos only — skip for stdio-only MCP servers)

In your `requirements.txt`, add:
```
opentelemetry-instrumentation-fastapi==0.63b1
opentelemetry-instrumentation-requests==0.63b1
```
In your app entrypoint, right after `app = FastAPI(...)`:
```python
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor

FastAPIInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()
```
This — combined with `@otel_trace` from §5 — is what actually produces spans. There is no separate "enable tracing" step beyond this plus `OTEL_TRACING_ENABLED=true`/`OTEL_REMOTE_TRACING=true` being set wherever your service actually runs (see §8b — don't just check your local `.env`, check the live pod's actual environment, since platform injection and a repo's checked-in `.env` can legitimately differ).

## 8. Target OpenSearch output shape (logs) — what to verify against

Once wired up and running with `OTEL_ENABLED=true` (plus whichever `OTEL_*_ENABLED`/`OTEL_REMOTE_*` variables you need — see `cams-otel-lib`'s README), a `Logger.log_event(...)` call should produce a record carrying, at minimum:
```json
{
  "agent_instance_id": "550e8400-e29b-41d4-a716-446655440000",
  "scope": "system.finance.settlement",
  "tenant_id": "1",
  "message_category": "LLM",
  "message_subtype": "LLM_REQUEST",
  "timestamp": "2026-02-10T14:35:22.150Z",
  "log_level": "INFO",
  "retry_count": 1,
  "message": "LLM request sent to gpt-4",
  "app_id": "edp-settlement",
  "session_id": "sess-789",
  "attributes": {
    "model": "gpt-4",
    "otel.trace_id": "...",
    "otel.span_id": "...",
    "otel.parent_span_id": "..."
  }
}
```
plus `system_id`/`org_id`/`project_id` (the decomposed scope, kept alongside `scope` so OpenSearch can filter/aggregate by any single level without parsing the dotted string), and any optional fields you set (`chat_conversation_id`, `correlation_id`, `user_id`, `parent_request_id`, `agent_harness_id`, `knowledge_base_name`).

Note: the mandatory schema's `message` field maps to the OTEL log record's `body` (the first positional argument to the underlying log call) rather than a literal `extra`-dict key — `"message"`/`"asctime"` are reserved `LogRecord` attribute names in Python's stdlib `logging` and will raise `KeyError` if put in `extra` directly. Whatever exports/indexes these records into OpenSearch should map `body` → `message` — this is standard OTEL Log Data Model behavior, not something to work around in your own code.

## 8b. Target OpenSearch output shape (traces) — what to verify against, and the gotchas that make working traces look broken

This section didn't exist in v2/v3. Its absence was a real, confirmed source of confusion: the instrumentation in §5/§7 works and produces correctly-formed spans, but there was no documented way to confirm that, so a correctly-wired integration and a silently-broken one looked identical from the integrator's side.

**Where traces land:** with the current OpenSearch exporter/collector config (`opensearchexporter`, default `mapping_mode`, `dataset: otel`, `namespace: default`), spans are written to an index named **`ss4o_traces-otel-default`** — a completely different index from where logs land (`logs-otel-v1`). If you go looking for traces in the logs index, you won't find them; that's expected, not a bug. If your platform's collector config changes `dataset`/`namespace`, the index name changes accordingly (`ss4o_traces-{dataset}-{namespace}`) — check the live collector config if unsure rather than assuming the name above.

**The `@timestamp` gotcha (confirmed, not a config mistake you can fix in your service):** at the collector version currently in use, every span document's `@timestamp` field is left unset and serializes as the Go zero-value, `"0001-01-01T00:00:00Z"`. This is a real gap in that exact `opensearchexporter` version's trace-encoding code, not something wrong with your instrumentation, your `RequestContext`, or your OTLP export. **Do not use `@timestamp` to find or sort your traces — use `startTime` instead.** When creating an OpenSearch index pattern for traces, set the time field to `startTime`, not `@timestamp`. If you query the index directly, sort/filter on `startTime`/`endTime`, which are populated correctly with real values.

**Expected span document shape** (a single span from a real HTTP request, as actually produced by `FastAPIInstrumentor`/`RequestsInstrumentor` + `@otel_trace`):
```json
{
  "traceId": "8ea8d4352b4d0c17727c7e32be19d69c",
  "spanId": "100c3579f1456fec",
  "parentSpanId": "",
  "name": "GET /api/v1/tool-credentials/{tool_name}/resolve",
  "kind": "Server",
  "startTime": "2026-07-27T09:10:18.102426892Z",
  "endTime": "2026-07-27T09:10:18.205939932Z",
  "status": { "code": "Unset", "message": "" },
  "droppedAttributesCount": 0,
  "droppedEventsCount": 0,
  "droppedLinksCount": 0,
  "@timestamp": "0001-01-01T00:00:00Z",
  "traceState": "",
  "attributes": {
    "http.method": "GET",
    "http.route": "/api/v1/tool-credentials/{tool_name}/resolve",
    "http.status_code": 200,
    "http.url": "http://your-service.dev.cams.covasant.io/api/v1/...",
    "net.peer.ip": "10.1.3.12"
  },
  "instrumentationScope": {
    "name": "opentelemetry.instrumentation.fastapi",
    "version": "0.63b1",
    "schemaUrl": "https://opentelemetry.io/schemas/1.11.0"
  },
  "resource": {
    "service.name": "your-service-name",
    "service.namespace": "controltower",
    "environment": "development",
    "telemetry.sdk.language": "python",
    "telemetry.sdk.name": "opentelemetry",
    "telemetry.sdk.version": "1.42.1"
  }
}
```
A single incoming HTTP request produces **multiple** span documents sharing one `traceId`, not one: a root `kind: "Server"` span (empty `parentSpanId`) plus several `kind: "Internal"` child spans for the ASGI lifecycle (`http.request` receive, `http.response.start`/`.body` send events, etc.), all with `parentSpanId` pointing back to the root span's `spanId`. This is correct and expected, not duplication — when verifying, check that the child spans' `parentSpanId` values actually resolve to a `spanId` present in the same `traceId`, confirming the trace tree is intact rather than a pile of disconnected spans.

**How to actually verify (don't skip this — do it for every integration, the same way §8 requires for logs):**
1. Send a real request/tool-call through your service with something identifiable in the URL/path (a distinct query param or route).
2. Query the traces index for a recent document where `resource.service.name` matches your service and `attributes`/`name` matches what you just triggered (sort/filter by `startTime`, not `@timestamp`).
3. Pull every span sharing that document's `traceId` and confirm the parent/child chain resolves back to a single root span — not just that "a span exists somewhere."
4. Confirm `OTEL_TRACING_ENABLED`/`OTEL_REMOTE_TRACING` are actually `true` **in the environment your service is really running in** (`kubectl exec ... -- printenv`, not just the repo's checked-in `.env` — platform-injected secrets and a repo's local `.env` can and do differ, and only the former matters at runtime).
5. If nothing shows up at all: check your own service's logs first for `Failed to export traces` / `DEADLINE_EXCEEDED`-style errors before assuming your code is wrong — this can just as easily be the shared collector or OpenSearch cluster being unhealthy, which no amount of correct instrumentation code will work around. Confirm with someone who has cluster access whether *other* services' traces are currently landing before spending time re-debugging your own integration.

## 9. Finally

Generate or update an `Otel_Transformation.md` in your repo recording what was actually done — which pattern (4a/4b/4c) you used, which files changed, where each identity field is sourced from in your specific repo, and which `message_category`/`message_subtype` events you wired in (and which, like `AI_SERVICE`, you deliberately left unwired because no real call site exists yet). Also record whether you completed the §8b trace verification steps and what you found (index name, a confirmed multi-span trace, or a known blocker if traces weren't landing for cluster-side reasons outside your service). Keep it in sync with the real code going forward — a stale doc that no longer matches the shipped implementation is worse than no doc.
