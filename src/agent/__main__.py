"""
Agent server entry point.
Run with: python -m src.agent
"""

import asyncio
import contextlib
import json
import os
import sys

import uvicorn
from fastapi import Request, Body, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.apps import A2AFastAPIApplication
from a2a.types import AgentCard, AgentCapabilities, AgentProvider, AgentSkill
from platform_authlib.client import AuthClient
from platform_authlib.middleware import AuthEnforcementMiddleware

from pathlib import Path

from .executor import AgentExecutor
from .rbac import mentions_restricted_topic, is_admin
from src.config.settings import settings
from src.middleware.claims_middleware import OtelContextMiddleware
from src.utils.health import get_health_checker
from cams_otel_lib import Logger as logger, Otel_Client, otel_trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

if not settings.auth_service_url:
    raise ValueError(
        "auth_service_url must be set — it supplies the auth-service base "
        "URL used to verify JWTs and evaluate the permission bound to "
        "POST /agent/run in auth-config.json. "
        "Set AUTH_SERVICE_URL in this agent's environment/ConfigMap."
    )

# Checks every request against auth-service via authlib's shared
# AuthEnforcementMiddleware (see the app.add_middleware call in main() below).
# Constructed once at import time and reused for every request; initialised
# (auth.initialize()) and closed in main() below.
auth = AuthClient(
    auth_service_url=settings.auth_service_url,
    app_id=settings.auth_app_id,
)


@otel_trace
def _read_agent_config_field(field: str, default: str = "N/A") -> str:
    """Read a top-level field from agent_config.json. APP_CONFIG_PATH takes priority."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        if ext:
            p = Path(ext)
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
                    # Support both top-level and nested runtime_context
                    return data.get(field) or data.get("runtime_context", {}).get(field, default)
        config_path = Path(__file__).parent.parent / "config" / "agent_config.json"
        with open(config_path) as f:
            data = json.load(f)
            return data.get(field) or data.get("runtime_context", {}).get(field, default)
    except Exception:
        return default







@otel_trace
def create_agent_card() -> AgentCard:
    """Build the A2A agent card from agent_config.json agent_definition."""
    from src.config.cams_config_adapter import CAMSConfigAdapter

    agent_def = {}
    capabilities_cfg = {}
    provider_cfg = {}
    skills_cfg = []
    raw_version = "1.0.0"
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            import json as _json
            raw = _json.loads(cfg_path.read_text())
            adapter = CAMSConfigAdapter(raw)
            agent_def = adapter.get_agent_definition()
            capabilities_cfg = agent_def.get("capabilities", {})
            provider_cfg = agent_def.get("provider", {})
            skills_cfg = agent_def.get("skills", [])
            raw_version = agent_def.get("version", "1.0.0")
    except Exception as e:
        logger.warning(f"Could not load agent_definition from config, using defaults: {e}")

    if skills_cfg:
        skills = [
            AgentSkill(
                id=s.get("id", f"skill_{i}"),
                name=s.get("name", f"Skill {i}"),
                description=s.get("description", ""),
                tags=s.get("tags", []),
                examples=s.get("examples", []),
                input_modes=s.get("inputModes", ["text/plain"]),
                output_modes=s.get("outputModes", ["text/plain"]),
            )
            for i, s in enumerate(skills_cfg)
        ]
    else:
        skills = [
            AgentSkill(
                id="chat",
                name="Chat",
                description="Answer questions conversationally, calling tools when needed",
                tags=["chat", "assistant"],
                examples=["What is 12 * 7?", "How many words are in this sentence?"],
                input_modes=["text/plain"],
                output_modes=["text/plain"],
            ),
        ]

    return AgentCard(
        name=agent_def.get("name") or settings.agent_name,
        description=agent_def.get("description") or settings.agent_description,
        url=settings.agent_url,
        version=raw_version,
        protocol_version="0.3.0",
        preferred_transport="HTTP+JSON",
        default_input_modes=agent_def.get("defaultInputModes", ["text/plain"]),
        default_output_modes=agent_def.get("defaultOutputModes", ["text/plain"]),
        capabilities=AgentCapabilities(
            streaming=capabilities_cfg.get("streaming", settings.streaming_enabled),
            push_notifications=capabilities_cfg.get("push_notifications", False),
        ),
        skills=skills,
        supports_authenticated_extended_card=False,
        provider=AgentProvider(
            organization=provider_cfg.get("organization", "CAMS"),
            url=provider_cfg.get("url", ""),
        ),
        documentation_url=None,
        icon_url=None,
    )


async def _wake_loop(interval_seconds: int = 10) -> None:
    """Background heartbeat — wakes up every `interval_seconds` and logs a line."""
    while True:
        await asyncio.sleep(interval_seconds)
        logger.info("Wake loop cycle: agent is alive")


async def main():
    """Main entry point for the agent server."""
    Otel_Client.get_current_otel_client() or Otel_Client.initialize_otel_client(
        service_name=settings.agent_name,
        environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
        agent_id=_read_agent_config_field("instance_id", default="N/A"),
    )

    RequestsInstrumentor().instrument()
    HTTPXClientInstrumentor().instrument()

    try:
        await auth.initialize()
        logger.info("Auth client initialised")
    except Exception as exc:
        logger.error(f"Auth client initialisation failed: {exc}")
        if os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")) != "dev":
            raise

    logger.info(f"Starting agent: {settings.agent_name} on {settings.host}:{settings.port}")

    agent_card = create_agent_card()
    executor = AgentExecutor()

    task_store = InMemoryTaskStore()
    request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
    )

    a2a_app = A2AFastAPIApplication(
        agent_card=agent_card,
        http_handler=request_handler,
    )

    app = a2a_app.build()

    FastAPIInstrumentor().instrument_app(app)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {})
        schema["components"]["securitySchemes"] = {
            "BearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
            }
        }
        schema["security"] = [{"BearerAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    # Requires a valid JWT on every request. Only POST /agent/run has a
    # declared resource binding in auth-config.json, checked centrally by
    # auth-service via POST /auth/check; every other route just needs a
    # valid token, per auth-config.json's default_policy: allow for
    # unbound paths.
    app.add_middleware(AuthEnforcementMiddleware, auth_client=auth)

    app.add_middleware(OtelContextMiddleware)

    # CORSMiddleware must be the LAST app.add_middleware call in this
    # function (Starlette middleware is LIFO: last-added = outermost), so it
    # wraps every other middleware above — including AuthEnforcementMiddleware
    # — and stamps Access-Control-Allow-Origin on every response they produce,
    # including 401/403 denials. Added earlier in this list, those
    # middlewares' own error responses never pass back through CORSMiddleware,
    # and browsers block the frontend from reading them entirely (reported as
    # a generic "Failed to fetch").
    #
    # Deliberately wildcarded (allow_origins=["*"]) rather than restricted to specific
    # CAMS frontend origins — allow_credentials must stay False, since Starlette forces
    # that combination (a literal "*" origin can't be paired with credentialed requests).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    health_checker = get_health_checker()

    @app.get("/health")
    async def health_check():
        health_status = await health_checker.get_health_status()
        status_code = 503 if health_status["status"] == "unhealthy" else 200
        from fastapi import Response
        return Response(
            content=json.dumps(health_status),
            status_code=status_code,
            media_type="application/json",
        )

    @app.get("/health/ready")
    async def readiness_check():
        is_ready = await health_checker.is_ready()
        if is_ready:
            return {"status": "ready"}
        from fastapi import Response
        return Response(
            content='{"status": "not ready"}',
            status_code=503,
            media_type="application/json",
        )

    @app.get("/health/live")
    async def liveness_check():
        is_alive = await health_checker.is_alive()
        if is_alive:
            return {"status": "alive"}
        from fastapi import Response
        return Response(
            content='{"status": "not alive"}',
            status_code=503,
            media_type="application/json",
        )



    @app.post("/agent/run")
    async def agent_run_endpoint(http_request: Request, body: dict = Body(...)):
        """
        Direct agent execution endpoint.

        Request format:
        {
            "query": "Your question here",
            "conversation_id": "optional-conversation-id"
        }

        Response format:
        {
            "response": "Agent's answer",
            "instance_id": "...",
            "conversation_id": "...",
            "user_id": "..."
        }
        """
        try:
            import uuid
            from langchain_core.messages import HumanMessage

            query = body.get("query", "")
            if not query:
                return {"error": "Query is required"}

            # Topic-based RBAC (this agent's own Layer 2, on top of
            # AuthEnforcementMiddleware's Layer 1 token check): "Covasant" is
            # restricted to admin/System Administrator roles. Every other
            # topic is open to any authenticated caller regardless of role.
            if mentions_restricted_topic(query) and not is_admin(http_request):
                logger.info(
                    f"Blocked restricted-topic chat request: caller lacks admin role, query_length={len(query)}"
                )
                return JSONResponse(
                    {
                        "error": "insufficient_permissions",
                        "message": (
                            "Discussing Covasant requires an admin or System "
                            "Administrator role. Contact your admin to request access."
                        ),
                    },
                    status_code=403,
                )

            # Runtime context: request params/body take priority over baked config
            instance_id = _read_agent_config_field("instance_id", default="N/A")
            tenant_id = (
                str(http_request.query_params.get("tenant_id") or body.get("tenant_id") or "")
                or _read_agent_config_field("tenant_id", default="default")
            )
            user_id = (
                str(http_request.query_params.get("user_id") or body.get("user_id") or "")
                or _read_agent_config_field("user_id", default="")
            )

            thread_id = body.get("conversation_id") or f"thread_{uuid.uuid4().hex[:16]}"

            logger.info(f"Agent request received: query_length={len(query)}, conversation_id={thread_id}")

            trace_id = uuid.uuid4().hex
            parent_span_id = uuid.uuid4().hex[:16]

            initial_state = {
                "messages": [HumanMessage(content=query)],
                "final_response": "",
                "tenant_id": tenant_id,
                "thread_id": thread_id,
                "_langfuse_trace_id": trace_id,
                "_langfuse_span_id": parent_span_id,
                "litellm_headers": None,
            }

            final_state = await executor._run_graph(initial_state, tenant_id, thread_id)
            response_text = final_state.get("final_response", "No response generated")

            logger.info(f"Agent response generated: response_length={len(response_text)}, conversation_id={thread_id}")

            return {
                "response": response_text,
                "instance_id": instance_id,
                "conversation_id": thread_id,
                "user_id": user_id,
            }

        except Exception as e:
            logger.error(f"Agent error: {str(e)}")
            return {"error": str(e)}

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
    )

    server = uvicorn.Server(config)
    logger.info(f"Agent ready at http://{settings.host}:{settings.port}")

    wake_loop_task = asyncio.create_task(_wake_loop())

    try:
        await server.serve()
    except KeyboardInterrupt:
        logger.info("Shutting down gracefully")
    finally:
        wake_loop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await wake_loop_task
        await auth.close()
        logger.info("Auth client closed")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Agent stopped by user")
        sys.exit(0)
