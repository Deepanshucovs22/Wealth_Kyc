"""
Application settings from environment variables.
Customize this file to add your own configuration options.
"""

import json
import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings


# Env vars the hosting platform owns rather than this agent's config. Compared
# case-insensitively, since the config's env block spells these lowercase
# ("port"/"host") while a platform injects them uppercase.
_PLATFORM_OWNED_ENV = {"PORT", "HOST"}


def _deep_fill_missing(base: dict, fallback: dict) -> None:
    """
    Fill keys in `base` that are absent, None, or "" from `fallback`, recursively
    for nested dicts. `base`'s own non-empty values always win — this only fills
    gaps. Canonical implementation for the repo; cams_config_adapter imports it
    rather than keeping a second copy that could drift.
    """
    for key, fallback_value in fallback.items():
        if key not in base or base[key] is None or base[key] == "":
            base[key] = fallback_value
        elif isinstance(base[key], dict) and isinstance(fallback_value, dict):
            _deep_fill_missing(base[key], fallback_value)


def _read_json(path: Path) -> dict:
    """Read a JSON config file. Returns {} if missing/unreadable — never raises.

    settings.py is imported before logging is configured, so this stays silent
    rather than importing a logger (which would also risk an import cycle:
    cams_config_adapter imports from this module).
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_effective_config_dict() -> dict:
    """
    Layer every config source into one dict. Highest priority first:

      1. agent_config.json (CAMS-mounted)  — located via APP_CONFIG_PATH
      2. agent_config.json (baked into the image) — src/config/agent_config.json
      3. local.agent_config.json — gitignored, local-dev only, absent in any
         real deployment

    Each lower layer only fills keys the higher layers left absent/None/"", so a
    CAMS config that omits a block (or supplies a PARTIAL one) still falls back
    to the baked-in defaults instead of shadowing them wholesale. That partial
    case is the one that bites: a CAMS UI export commonly carries a secrets.env
    with a couple of keys and no AUTH_SERVICE_URL — picking that block whole
    would leave the agent with no auth URL and kill it at import time.
    """
    paths = []
    app_config_path = os.getenv("APP_CONFIG_PATH")
    if app_config_path:
        paths.append(Path(app_config_path))
    paths.append(Path(__file__).parent / "agent_config.json")
    paths.append(Path(__file__).resolve().parents[2] / "local.agent_config.json")

    effective: dict = {}
    for path in paths:
        layer = _read_json(path)
        if layer:
            _deep_fill_missing(effective, layer)
    return effective


def _load_config_secrets_env() -> dict:
    """The agent_config.secrets.env block, resolved across all config layers."""
    env = (
        load_effective_config_dict()
        .get("agent_config", {})
        .get("secrets", {})
        .get("env")
    )
    return env or {}


def _apply_config_env() -> None:
    """Bridge agent_config.json's secrets.env into os.environ before Settings()
    reads it. A non-empty config value overrides any ambient env var — this agent
    runs as a pod on the CAMS platform, where ambient/platform-wide env vars must
    not silently win over this agent's explicit per-agent config. An empty config
    value uses setdefault so it never clobbers a genuinely platform-injected
    secret."""
    for key, value in _load_config_secrets_env().items():
        if key.upper() in _PLATFORM_OWNED_ENV:
            # HOST/PORT are the one legitimate exception to config-wins. They are
            # not ambient platform junk — they are the platform TELLING the app
            # where to bind, and the Service/ingress routes to that same port.
            # Overriding an injected PORT makes the app bind somewhere nothing
            # routes to: the pod runs and never crashes, but every request gets
            # 502 and the deploy health gate never goes green.
            #
            # Check BOTH spellings before defaulting: env vars are case-sensitive
            # on Linux, the config spells these lowercase ("port") and platforms
            # inject them uppercase ("PORT"). A plain setdefault(key) would write
            # a lowercase "port" alongside the platform's "PORT", leaving two
            # different values that pydantic's case-insensitive lookup picks
            # between arbitrarily — which is how the injected port got ignored.
            if key not in os.environ and key.upper() not in os.environ:
                os.environ[key] = str(value)
        elif value not in (None, ""):
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
