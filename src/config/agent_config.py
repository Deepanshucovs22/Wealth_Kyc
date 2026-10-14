"""
Agent configuration loading with CAMS schema support.
"""

import json
from pathlib import Path
from typing import Dict, Any, Optional

from src.config.cams_config_adapter import load_cams_config
from src.config.settings import settings, _deep_fill_missing
from cams_otel_lib import Logger as logger, otel_trace

_MULTI_TENANT = settings.multi_tenant_enabled



@otel_trace
def _deep_merge(base: Dict[str, Any], overrides: Dict[str, Any]) -> None:
    """Merge overrides into base in-place. Nested dicts are merged; other types are overwritten."""
    for key, value in overrides.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


@otel_trace
def _read_json_config(path: Path) -> Optional[Dict[str, Any]]:
    """Read and parse a JSON config file. Returns None if missing/unreadable — never raises."""
    try:
        with open(path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        logger.error(f"Error parsing config file {path}: {e}")
        return None


@otel_trace
def load_agent_config(config_path: Optional[Path] = None) -> Dict[str, Any]:
    """
    Load agent configuration (CAMS schema), layering the CAMS-mounted config over
    the baked-in image config rather than picking one or the other.

    Priority: CAMS-mounted config (config_path, or settings.app_config_path) wins
    field-for-field, but the baked-in src/config/agent_config.json fills any
    block/field the CAMS-mounted config omits (e.g. a cams_v2 export from the CAMS
    UI typically has no `secrets`/`tools` — an either/or load would silently drop
    those instead of falling back to the image's own defaults).

    Args:
        config_path: Path to the CAMS-mounted config (overrides settings.app_config_path)

    Returns:
        Configuration dictionary in internal format
    """
    internal_path = Path(__file__).parent / "agent_config.json"
    external_path: Optional[Path] = None
    if config_path is not None:
        external_path = Path(config_path)
    elif settings.app_config_path:
        external_path = Path(settings.app_config_path)

    internal_raw = _read_json_config(internal_path)

    # Layer 3 (lowest): local.agent_config.json — gitignored, local-dev only and
    # absent in any real deployment. It fills only what the baked-in config
    # leaves blank, which is how a developer supplies real credentials without
    # the image ever carrying them (the committed agent_config.json ships with
    # every credential field blanked).
    local_path = Path(__file__).resolve().parents[2] / "local.agent_config.json"
    local_raw = _read_json_config(local_path)
    if local_raw:
        logger.info(f"Layering local developer config from: {local_path}")
        internal_raw = internal_raw or {}
        _deep_fill_missing(internal_raw, local_raw)

    if external_path and external_path != internal_path:
        logger.info(f"Attempting to load CAMS-mounted config from: {external_path}")
        external_raw = _read_json_config(external_path)
        if external_raw is not None:
            logger.info(
                f"✓ Loaded CAMS-mounted config from {external_path}; "
                f"merging with baked-in defaults from {internal_path}"
            )
            return load_cams_config(external_raw, fallback_config=internal_raw or {})
        logger.warning(f"CAMS-mounted config not found/unreadable at {external_path}, falling back to baked-in config")

    if internal_raw is not None:
        logger.info(f"✓ Using baked-in agent configuration from: {internal_path}")
        return load_cams_config(internal_raw)

    logger.error(f"No agent_config.json found at {internal_path} or {external_path}")
    return {"default": {}}


@otel_trace
def get_node_configuration(
    node_name: str, tenant_id: str, config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Get node-specific configuration for a tenant.

    Args:
        node_name: Name of the node (e.g., "search_query", "response")
        tenant_id: Tenant identifier
        config: Full configuration dictionary

    Returns:
        Node-specific configuration
    """
    effective_tenant = tenant_id if _MULTI_TENANT else "default"
    tenant_config = config.get(effective_tenant, config.get("default", {}))

    # Check for node-specific config
    node_config = tenant_config.get(f"{node_name}_config", {})

    return node_config


@otel_trace
def get_secrets(tenant_id: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get secrets configuration for a tenant.
    
    Secrets include: Langfuse, Sentry, Database, LiteLLM, GCP, Pinecone, etc.
    API keys (OpenAI, Anthropic, Google) come from environment variables.

    Args:
        tenant_id: Tenant identifier
        config: Full configuration dictionary

    Returns:
        Secrets configuration dictionary
    """
    effective_tenant = tenant_id if _MULTI_TENANT else "default"
    tenant_config = config.get(effective_tenant, config.get("default", {}))
    secrets = tenant_config.get("secrets", {})

    return secrets


# FEATURE:kubernetes_configmap
@otel_trace
def _merge_kubernetes_configmap(internal_config: Dict[str, Any]) -> None:
    """
    Merge per-tenant config and secrets from Kubernetes ConfigMap into internal_config.

    ConfigMap overrides take priority over values in agent_config.json so that
    CAMS can inject tenant-specific settings at deploy time without rebuilding
    the Docker image.

    Only runs when ENV_ID, AGENT_ID, and VERSION_ID environment variables are set
    (i.e. inside a real Kubernetes deployment).
    """
    config_loader = get_config_loader()
    secrets_loader = get_secrets_loader()

    if not config_loader.is_k8s_deployment():
        logger.debug("Not a K8s deployment — skipping ConfigMap merge")
        return

    tenant_ids = config_loader.tenant_ids or list(internal_config.keys())
    logger.info(f"Merging Kubernetes ConfigMap overrides for tenants: {tenant_ids}")

    for tenant_id in tenant_ids:
        if tenant_id not in internal_config:
            continue

        tenant_overrides = config_loader.load_tenant_config(tenant_id)
        if tenant_overrides:
            _deep_merge(internal_config[tenant_id], tenant_overrides)
            logger.info(f"Applied ConfigMap config overrides for tenant: {tenant_id}")

        tenant_secrets = secrets_loader.load_tenant_secrets(tenant_id)
        if tenant_secrets:
            existing_secrets = internal_config[tenant_id].get("secrets", {})
            _deep_merge(existing_secrets, tenant_secrets)
            internal_config[tenant_id]["secrets"] = existing_secrets
            logger.info(f"Applied ConfigMap secrets overrides for tenant: {tenant_id}")
