"""
CAMS Configuration Adapter
Converts CAMS agent config schema to internal format for backward compatibility.
"""

import copy
from typing import Dict, Any, Optional

from cams_otel_lib import Logger as logger, otel_trace


@otel_trace
def _deep_fill_missing(base: Dict[str, Any], fallback: Dict[str, Any]) -> None:
    """
    Fill keys in `base` that are absent, None, or "" from `fallback`, recursively
    for nested dicts. `base`'s own non-empty values always win — this only fills gaps.
    """
    for key, fallback_value in fallback.items():
        if key not in base or base[key] is None or base[key] == "":
            base[key] = fallback_value
        elif isinstance(base[key], dict) and isinstance(fallback_value, dict):
            _deep_fill_missing(base[key], fallback_value)


class CAMSConfigAdapter:
    """
    Adapter to convert CAMS agent configuration schema to internal format.

    This allows the agent code to work with the CAMS schema while maintaining
    backward compatibility with existing node implementations.
    """

    @otel_trace
    def __init__(self, cams_config: Dict[str, Any], fallback_config: Optional[Dict[str, Any]] = None):
        """
        Initialize with CAMS configuration.

        Args:
            cams_config: The CAMS-mounted config (or baked-in config when there's no
                external one) — takes priority over fallback_config on every field.
            fallback_config: The baked-in src/config/agent_config.json, used to fill
                any block/field the CAMS-mounted config omits (e.g. a cams_v2 export
                from the CAMS UI typically has no `secrets`/`tools`). None when
                cams_config already IS the baked-in config — nothing to fall back to.
        """
        self.cams_config = cams_config
        self.fallback_config = fallback_config or {}

    @otel_trace
    def get_tenant_config(self, tenant_id: str = "default") -> Dict[str, Any]:
        """
        Get tenant-specific configuration in internal format.

        Args:
            tenant_id: Tenant identifier

        Returns:
            Configuration dictionary compatible with existing node code
        """
        # Find tenant config
        tenant_configs = self.cams_config.get("tenant_config", [])
        tenant_config = None

        for tc in tenant_configs:
            if tc.get("tenant_name") == tenant_id:
                tenant_config = tc
                break

        # Fallback to default tenant
        if not tenant_config:
            for tc in tenant_configs:
                if tc.get("tenant_name") == "default":
                    tenant_config = tc
                    break

        # Fallback to root agent_config
        if not tenant_config:
            # Detect format: A2A card (extensions.cams_v2) vs scaffold format (agent_config)
            cams_v2 = self.cams_config.get("extensions", {}).get("cams_v2", {})
            if cams_v2:
                agent_config = self._extract_from_cams_v2(cams_v2)
            else:
                # dict(...) so we never mutate the caller's original dict via _deep_fill_missing
                agent_config = dict(self.cams_config.get("agent_config", {}))

            # A CAMS-mounted config (cams_v2 export or plain agent_config) commonly omits
            # whole blocks (secrets/tools/prompts) — fill those gaps from the baked-in
            # config instead of silently dropping them (e.g. litellm looking disabled).
            if self.fallback_config:
                fallback_agent_config = copy.deepcopy(self.fallback_config.get("agent_config", {}))
                # prompts.system in the baked-in config is that config's own mirror of ITS
                # OWN "instructions" field. If the CAMS side overrides "instructions" but
                # (as cams_v2 exports typically do) has no "prompts" block of its own,
                # filling "prompts" wholesale from the baked-in config would silently
                # resurrect the baked-in instructions via prompts.system, clobbering the
                # override that already landed in agent_config["instructions"] above.
                # _convert_to_internal_format always derives prompts.system from
                # "instructions" first — so drop the fallback's prompts.system and let
                # only its other prompt keys (retrieval/response templates) fill gaps.
                fallback_agent_config.get("prompts", {}).pop("system", None)
                _deep_fill_missing(agent_config, fallback_agent_config)
        else:
            agent_config = tenant_config.get("agent_config", {})

        # Convert to internal format
        return self._convert_to_internal_format(agent_config)

    @otel_trace
    def _extract_from_cams_v2(self, cams_v2: Dict[str, Any]) -> Dict[str, Any]:
        """
        Map A2A card extensions.cams_v2 → scaffold agent_config shape
        so _convert_to_internal_format can process either format.
        """
        llm_cfg = cams_v2.get("llm_config", {})
        return {
            "agent_id":       cams_v2.get("app_id", ""),
            "agent_name":     self.cams_config.get("name", ""),
            "app_name_adk":   cams_v2.get("app_name_adk", ""),
            "instructions":   cams_v2.get("instructions", ""),
            # Leave model/temperature/max_tokens/provider ABSENT-as-None (not a hardcoded
            # non-empty default like "gpt-4o"/"openai") when cams_v2 doesn't specify them —
            # a non-empty value here would shadow the baked-in config's own model/provider
            # in _deep_fill_missing, which only fills gaps that are absent/None/"".
            "llm_config": {
                "model":       llm_cfg.get("model"),
                "temperature": llm_cfg.get("temperature"),
                "max_tokens":  llm_cfg.get("max_tokens"),
            },
            # provider may be stored as llm_config.provider or top-level
            "llm_provider":   llm_cfg.get("provider") or cams_v2.get("llm_provider"),
            "max_steps":      cams_v2.get("max_steps", 10),
            "max_iterations": cams_v2.get("max_iterations", 5),
            "tools":          cams_v2.get("tools", []),
            # knowledge_bases is the A2A card name for datastores
            "datastores":     cams_v2.get("knowledge_bases", cams_v2.get("datastores", [])),
            "output_key":     cams_v2.get("output_key", "final_response"),
            "subagent_configs": cams_v2.get("subagent_configs", []),
            "domain_knowledge": cams_v2.get("domain_knowledge", {}),
            "prompts":        cams_v2.get("prompts", {}),
            "secrets":        cams_v2.get("secrets", {}),
            "edp":            cams_v2.get("edp", {}),
        }

    @otel_trace
    def _convert_to_internal_format(self, agent_config: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert CAMS agent_config to internal format.
        
        CAMS Schema:
        {
          "agent_name": "...",
          "instructions": "...",
          "llm_config": {"model": "...", "temperature": 0.3},
          "llm_provider": "openai",
          "tools": [...],
          "datastores": [...]
        }
        
        Internal Format:
        {
          "agent_name": "...",
          "prompts": {
            "system": "...",
            "retrieval": "...",
            "response": "..."
          },
          "llm_config": {
            "retrieval": {"model": "...", "temperature": 0.1, "provider": "..."},
            "response": {"model": "...", "temperature": 0.3, "provider": "..."}
          }
        }
        """
        # Extract base values
        agent_name = agent_config.get("agent_name", "CAMS Agent")
        instructions = agent_config.get("instructions", "You are a helpful AI assistant.")
        llm_config = agent_config.get("llm_config", {}) or {}
        llm_provider = agent_config.get("llm_provider") or "openai"

        # `.get(key, default)` only applies `default` when the key is ABSENT — a key
        # present with value None (e.g. a cams_v2 export that left model unset and had
        # no baked-in fallback to fill it) would otherwise slip through as None. Use
        # `or` so a genuinely unset value always resolves to a real default.
        model = llm_config.get("model") or "gpt-4o"
        temperature = llm_config.get("temperature")
        temperature = temperature if temperature is not None else 0.3
        max_tokens_raw = llm_config.get("max_tokens")
        max_tokens = int(max_tokens_raw) if max_tokens_raw not in (None, "") else 4096

        # Build internal format
        internal_config = {
            "agent_name": agent_name,
            "description": agent_config.get("agent_id", ""),
            
            # Convert instructions to prompts
            "prompts": {
                "system": instructions,
                "retrieval": f"{instructions}\n\nAnalyze this query to extract the most relevant search terms.\n\nQuery: {{question}}\nContext: {{conversation}}\n\nExtract 2-5 specific, focused search terms:",
                "response": f"{instructions}\n\nRETRIEVED INFORMATION:\n{{context}}\n\nCONVERSATION HISTORY:\n{{conversation}}\n\nUSER QUESTION: {{question}}\n\nProvide a helpful response based on the information above."
            },
            
            # Convert LLM config to node-specific configs
            "llm_config": {
                "retrieval": {
                    "provider": llm_provider,
                    "model": model,
                    "temperature": 0.1,
                    "max_tokens": max_tokens,
                },
                "response": {
                    "provider": llm_provider,
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                # Used by AgentNode (ReAct graph) — same model as response by default
                "agent": {
                    "provider": llm_provider,
                    "model": model,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
            },

            # Pass through additional config
            "max_steps": int(agent_config.get("max_steps", 10)),
            "max_iterations": int(agent_config.get("max_iterations", 5)),
            "tools": agent_config.get("tools", []),
            "datastores": agent_config.get("datastores", []),
            "output_key": agent_config.get("output_key", "final_response"),

            # Pass through secrets (Langfuse, LiteLLM, etc.)
            "secrets": agent_config.get("secrets", {}),

            # Pass through domain knowledge — all sub-fields used by response_generator
            "domain_knowledge": agent_config.get("domain_knowledge", {}),

            # Pass through EDP config (repo-specific block some deployments use — the
            # stock adapter template silently drops this, don't let it happen here)
            "edp": agent_config.get("edp", {}),
        }
        
        # Override prompts if they exist in agent_config
        if "prompts" in agent_config:
            internal_config["prompts"].update(agent_config["prompts"])
        
        return internal_config
    
    @otel_trace
    def get_all_tenant_configs(self) -> Dict[str, Dict[str, Any]]:
        """
        Get all tenant configurations.
        
        Returns:
            Dictionary mapping tenant_id to configuration
        """
        configs = {}
        
        for tenant_config in self.cams_config.get("tenant_config", []):
            tenant_name = tenant_config.get("tenant_name", "default")
            agent_config = tenant_config.get("agent_config", {})
            configs[tenant_name] = self._convert_to_internal_format(agent_config)
        
        return configs
    
    @otel_trace
    def get_agent_definition(self) -> Dict[str, Any]:
        """Get agent definition from CAMS config."""
        return self.cams_config.get("agent_definition", {})
    
    @otel_trace
    def get_dependant_agents(self) -> list:
        """Get list of dependant agents."""
        return self.cams_config.get("dependantAgents", [])


@otel_trace
def load_cams_config(config: Dict[str, Any], fallback_config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Load CAMS configuration and convert to internal format.

    Args:
        config: CAMS configuration dictionary (CAMS-mounted, or the baked-in config
            itself when there's no external one)
        fallback_config: The baked-in src/config/agent_config.json, used to fill any
            block/field `config` omits. None when `config` already IS the baked-in one.

    Returns:
        Dictionary with tenant configurations in internal format
    """
    adapter = CAMSConfigAdapter(config, fallback_config=fallback_config)

    # Get all tenant configs
    tenant_configs = adapter.get_all_tenant_configs()

    # Add default config if not present
    if "default" not in tenant_configs:
        tenant_configs["default"] = adapter.get_tenant_config("default")

    logger.info(f"Loaded CAMS config with {len(tenant_configs)} tenant(s)")

    return tenant_configs
