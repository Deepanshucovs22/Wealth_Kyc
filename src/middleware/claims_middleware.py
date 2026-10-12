"""
OTEL Request Context Middleware — sets up per-request tracing context.

Initialises the OTEL RequestContext (request_id, tenant, scope, app/agent
identity) from request headers so every log line and span produced during
the request carries those attributes automatically. No authentication is
performed here — Pattern A (raw headers with config-file fallback) per the
OTEL Transformation v4 spec.

Context vars are reset after each request to prevent leakage across concurrent
async requests.
"""

import os
import uuid
import json
from pathlib import Path

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from cams_otel_lib import (
    Logger as logger,
    Otel_Client,
    otel_trace,
    RequestContext,
    MessageCategory,
    AuthenticationSubtype,
    LogLevel,
    set_request_context,
    set_observability_client,
    reset_request_context,
    reset_observability_client,
)
from src.config.settings import settings


@otel_trace
def _read_config_field(field: str, default: str = "N/A") -> str:
    """Read a field from agent_config.json, checking top-level then runtime_context."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = json.load(f)
                return data.get(field) or data.get("runtime_context", {}).get(field, default) or default
    except Exception:
        pass
    return default


@otel_trace
def _read_agent_definition_field(field: str, default: str = "N/A") -> str:
    """Read a field from agent_config.json's agent_definition block."""
    try:
        ext = os.getenv("APP_CONFIG_PATH")
        cfg_path = Path(ext) if ext else Path(__file__).parent.parent / "config" / "agent_config.json"
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = json.load(f)
                return data.get("agent_definition", {}).get(field) or default
    except Exception:
        pass
    return default


_CONFIG_TENANT_ID = _read_config_field("tenant_id")
_CONFIG_USER_ID = _read_config_field("user_id")
_CONFIG_SYSTEM_ID = _read_config_field("system_id")
_CONFIG_ORG_ID = _read_config_field("org_id")
_CONFIG_PROJECT_ID = _read_config_field("project_id")

# agent_id and app_id are the same underlying value for agents deployed through
# agent-registry (Registry's agent_id column is populated from the Workbench
# app_id at agent-creation time and carried forward across every version) — so
# both read from agent_definition.agent_id rather than two independent sources.
_CONFIG_AGENT_ID = _read_agent_definition_field("agent_id")
_CONFIG_APP_ID = _CONFIG_AGENT_ID

# Registry sets AGENT_INSTANCE_ID directly at deploy time for K8s/Cloud Run/Azure
# Functions targets; fall back to the config-file instance_id for older deployments.
_AGENT_INSTANCE_ID = os.getenv("AGENT_INSTANCE_ID") or _read_config_field("instance_id")


class OtelContextMiddleware(BaseHTTPMiddleware):
    """
    Middleware that wires up OTEL request context for every request.

    Reads optional headers (X-Request-ID, X-Tenant-ID, X-User-ID, X-App-Name,
    X-Session-ID, X-System-ID, X-Org-ID, X-Project-ID) and populates
    RequestContext so all logs and spans are automatically attributed.
    """

    @otel_trace
    async def dispatch(self, request: Request, call_next):
        request_context_token = None
        otel_client_token = None
        try:
            request_id = request.headers.get("X-Request-ID", uuid.uuid4().hex.lower())
            tenant_id = request.headers.get("X-Tenant-ID") or _CONFIG_TENANT_ID or "N/A"
            user_id = request.headers.get("X-User-ID") or _CONFIG_USER_ID or "N/A"
            app_id = request.headers.get("X-App-Name") or _CONFIG_APP_ID or "N/A"
            session_id = request.headers.get("X-Session-ID", uuid.uuid4().hex.lower())
            system_id = request.headers.get("X-System-ID") or _CONFIG_SYSTEM_ID or "N/A"
            org_id = request.headers.get("X-Org-ID") or _CONFIG_ORG_ID or "N/A"
            project_id = request.headers.get("X-Project-ID") or _CONFIG_PROJECT_ID or "N/A"

            request_context = RequestContext(
                request_id=request_id,
                tenant_id=tenant_id,
                app_id=app_id,
                user_id=user_id,
                session_id=session_id,
                agent_id=_CONFIG_AGENT_ID,
                agent_instance_id=_AGENT_INSTANCE_ID,
                system_id=system_id,
                org_id=org_id,
                project_id=project_id,
                service_name=settings.agent_name,
            )
            request_context_token = set_request_context(request_context)

            otel_client = Otel_Client.get_current_otel_client() or Otel_Client.initialize_otel_client(
                service_name=settings.agent_name,
                environment=os.getenv("ENVIRONMENT", os.getenv("ENV", "dev")),
                agent_id=_AGENT_INSTANCE_ID,
            )
            otel_client_token = set_observability_client(otel_client)

            logger.log_event(
                message_category=MessageCategory.AUTHENTICATION,
                message="Request identity/scope context established",
                message_subtype=AuthenticationSubtype.AUTH_SUCCESS,
                log_level=LogLevel.INFO,
                attributes={"tenant_id": tenant_id, "app_id": app_id, "user_id": user_id},
            )
        except Exception as e:
            logger.log_event(
                message_category=MessageCategory.AUTHENTICATION,
                message=f"Error setting up OTEL request context: {e}",
                message_subtype=AuthenticationSubtype.AUTH_FAILURE,
                log_level=LogLevel.ERROR,
            )

        try:
            response = await call_next(request)
        finally:
            if request_context_token:
                reset_request_context(request_context_token)
            if otel_client_token:
                reset_observability_client(otel_client_token)

        return response
