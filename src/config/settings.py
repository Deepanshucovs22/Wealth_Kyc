"""
Application settings from environment variables.
Customize this file to add your own configuration options.
"""

import json
import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings


def _load_config_secrets_env() -> dict:
    """Read agent_config.json's agent_config.secrets.env block — the CAMS-mounted
    config (APP_CONFIG_PATH) takes priority over the baked-in image config."""
    candidates = []
    app_config_path = os.getenv("APP_CONFIG_PATH")
    if app_config_path:
        candidates.append(Path(app_config_path))
    candidates.append(Path(__file__).parent / "agent_config.json")

    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
            env = data.get("agent_config", {}).get("secrets", {}).get("env")
            if env:
                return env
        except Exception:
            continue
    return {}


def _apply_config_env() -> None:
    """Bridge agent_config.json's secrets.env into os.environ before Settings()
    reads it. A non-empty config value overrides any ambient env var — this agent
    runs as a pod on the CAMS platform, where ambient/platform-wide env vars must
    not silently win over this agent's explicit per-agent config. An empty config
    value uses setdefault so it never clobbers a genuinely platform-injected
    secret."""
    for key, value in _load_config_secrets_env().items():
        if value not in (None, ""):
            os.environ[key] = str(value)
        else:
            os.environ.setdefault(key, "")


_apply_config_env()


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    CUSTOMIZE THIS: Add your own settings as needed.
    """

    # Server configuration
    host: str = "0.0.0.0"
    port: int = 9999
    log_level: str = "INFO"

    # Agent configuration
    agent_name: str = "LangGraph Agent"
    agent_description: str = "A customizable LangGraph-based AI agent"
    app_config_path: Optional[str] = None  # External config path for multi-instance support

    # A2A capabilities
    streaming_enabled: bool = True

    # LLM API keys - ONLY API keys come from environment/secret manager
    # All other secrets (Langfuse, Sentry, DB, LiteLLM) are in config.json
    openai_api_key: Optional[str] = None
    anthropic_api_key: Optional[str] = None
    google_api_key: Optional[str] = None

    # Tool API keys - add more as needed (these come from secret manager)
    tavily_api_key: Optional[str] = None
    serp_api_key: Optional[str] = None
    gcp_service_account_json: Optional[str] = None  # Path to service account JSON file
    pinecone_api_key: Optional[str] = None

    # PostgreSQL connection string (set when postgresql feature is selected)

    # Rate limiting (optional) - can be overridden in config
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 60
    rate_limit_per_hour: int = 1000
    rate_limit_burst_size: int = 10

    # Metrics (optional) - can be overridden in config
    metrics_enabled: bool = True

    # Multi-tenant support (False when feature not selected, True when selected via MULTI_TENANT_ENABLED=true)
    multi_tenant_enabled: bool = False

    # Auth — auth-service connection used by platform_authlib's shared
    # AuthEnforcementMiddleware (see src/agent/__main__.py's middleware
    # wiring). Required in deployed/production use; the app fails to
    # start without it.
    auth_service_url: Optional[str] = None
    auth_app_id: str = "cams"

    @property
    def agent_url(self) -> str:
        """Get the full agent URL."""
        return f"http://{self.host}:{self.port}"

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"  # Allow extra environment variables


# Global settings instance
settings = Settings()
