"""
LLM Provider Abstraction Layer

Supports multiple LLM providers:
- OpenAI (GPT-4, GPT-4o, GPT-3.5-turbo, etc.)
- Anthropic (Claude 3 Opus, Sonnet, Haiku)
- Google (Gemini Pro, Gemini 1.5 Pro, etc.)
"""

import json
import os
from typing import Optional, Union
from enum import Enum
from pydantic import SecretStr

from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_google_vertexai import ChatVertexAI
from google.oauth2 import service_account

from src.config.settings import settings
from src.config.agent_config import get_secrets, load_agent_config
from cams_otel_lib import Logger as logger, otel_trace

# Cache config to avoid reloading on every LLM creation
_cached_config = None
_cached_litellm_config = None
_cached_vertex_credentials = None

@otel_trace
def _get_litellm_config():
    """Get LiteLLM configuration from config.secrets.litellm"""
    global _cached_config, _cached_litellm_config
    
    if _cached_litellm_config is None:
        if _cached_config is None:
            _cached_config = load_agent_config()
        
        secrets = get_secrets("default", _cached_config)
        _cached_litellm_config = secrets.get("litellm", {})

    return _cached_litellm_config


@otel_trace
def _get_gcp_config():
    """Get GCP configuration from config.secrets.gcp"""
    global _cached_config

    if _cached_config is None:
        _cached_config = load_agent_config()

    return get_secrets("default", _cached_config).get("gcp", {})


@otel_trace
def _get_vertex_credentials():
    """Load the GCP service account credentials used for Vertex AI.

    Primary source is config.secrets.gcp.service_account — the key material lives
    in agent_config.json alongside the other secrets (db/litellm), so the container
    needs no mounted key file. GCP_SERVICE_ACCOUNT_JSON remains supported as a path
    to a key file for local runs that would rather not inline the key.

    Returns (credentials, project_id), or (None, None) when neither is configured —
    callers treat that as "no service account, use other auth methods instead."
    """
    global _cached_vertex_credentials
    if _cached_vertex_credentials is None:
        _cached_vertex_credentials = (None, None)

        sa_info = _get_gcp_config().get("service_account")
        # CAMS may deliver the block as a JSON string rather than a nested object.
        if isinstance(sa_info, str) and sa_info.strip():
            try:
                sa_info = json.loads(sa_info)
            except json.JSONDecodeError as e:
                logger.error(f"secrets.gcp.service_account is not valid JSON: {e}")
                sa_info = None

        sa_path = settings.gcp_service_account_json

        try:
            if isinstance(sa_info, dict) and sa_info.get("private_key"):
                credentials = service_account.Credentials.from_service_account_info(
                    sa_info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                logger.debug(
                    "Loaded Vertex AI service account from config.secrets.gcp.service_account "
                    f"({sa_info.get('client_email', 'unknown')})"
                )
                _cached_vertex_credentials = (credentials, credentials.project_id)
            elif sa_path:
                credentials = service_account.Credentials.from_service_account_file(
                    sa_path, scopes=["https://www.googleapis.com/auth/cloud-platform"]
                )
                logger.debug(f"Loaded Vertex AI service account from file: {sa_path}")
                _cached_vertex_credentials = (credentials, credentials.project_id)
        except Exception as e:
            # Never let a bad/incomplete key take the agent down — get_google_llm()
            # falls back to the LiteLLM gateway when this returns (None, None).
            logger.error(f"Failed to load Vertex AI service account credentials: {e}")

    return _cached_vertex_credentials


class LLMProvider(str, Enum):
    """Supported LLM providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"


# Default models for each provider
DEFAULT_MODELS = {
    LLMProvider.OPENAI: "gpt-4o",
    LLMProvider.ANTHROPIC: "claude-3-5-sonnet-20241022",
    LLMProvider.GOOGLE: "gemini-1.5-pro",
}


@otel_trace
def get_llm_model(
    provider: Union[str, LLMProvider],
    model_name: Optional[str] = None,
    temperature: float = 0.7,
    streaming: bool = True,
    max_tokens: Optional[int] = None,
    custom_headers: Optional[dict] = None,
    **kwargs,
):
    """
    Get LLM model instance for specified provider.

    Args:
        provider: LLM provider (openai, anthropic, google)
        model_name: Model name (optional, uses default if not provided)
        temperature: Temperature for generation (0.0-1.0)
        streaming: Enable streaming responses
        **kwargs: Additional provider-specific parameters

    Returns:
        LLM model instance (ChatOpenAI, ChatAnthropic, or ChatGoogleGenerativeAI)

    Raises:
        ValueError: If provider is unsupported or API key is missing

    Example:
        >>> llm = get_llm_model("openai", "gpt-4o", temperature=0.3)
        >>> llm = get_llm_model("anthropic", "claude-3-opus-20240229")
        >>> llm = get_llm_model("google", "gemini-1.5-pro")
    """
    # Convert string to enum if needed
    if isinstance(provider, str):
        try:
            provider = LLMProvider(provider.lower())
        except ValueError:
            raise ValueError(
                f"Unsupported provider: {provider}. "
                f"Supported providers: {[p.value for p in LLMProvider]}"
            )

    # Use default model if not specified
    if not model_name:
        model_name = DEFAULT_MODELS[provider]

    logger.debug(
        f"Creating LLM: provider={provider.value}, model={model_name}, "
        f"temperature={temperature}, streaming={streaming}"
    )

    # Dispatch to provider-specific function
    if provider == LLMProvider.OPENAI:
        return _create_openai_llm(model_name, temperature, streaming, custom_headers, max_tokens=max_tokens, **kwargs)
    elif provider == LLMProvider.ANTHROPIC:
        return _create_anthropic_llm(model_name, temperature, streaming, custom_headers, max_tokens=max_tokens, **kwargs)
    elif provider == LLMProvider.GOOGLE:
        return _create_google_llm(model_name, temperature, streaming, custom_headers, max_tokens=max_tokens, **kwargs)
    else:
        raise ValueError(f"Unsupported provider: {provider}")


@otel_trace
def _create_openai_llm(
    model_name: str, temperature: float, streaming: bool, custom_headers: Optional[dict] = None, max_tokens: Optional[int] = None, **kwargs
) -> ChatOpenAI:
    """Create OpenAI LLM instance with optional LiteLLM gateway support."""
    litellm_config = _get_litellm_config()
    litellm_enabled = litellm_config.get("enabled", False)
    litellm_base_url = litellm_config.get("base_url", "")

    api_key = settings.openai_api_key
    litellm_api_key = litellm_config.get("api_key", "")
    if not api_key and not (litellm_enabled and litellm_base_url):
        raise ValueError(
            "OPENAI_API_KEY environment variable is not set. "
            "Please set it in your .env file."
        )

    if litellm_enabled and litellm_base_url:
        logger.debug(f"Creating OpenAI LLM via LiteLLM gateway: {model_name}")
        llm_kwargs = {
            "api_key": SecretStr(litellm_api_key or api_key or "litellm-gateway"),
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "base_url": litellm_base_url,
        }
        if max_tokens is not None:
            llm_kwargs["max_tokens"] = max_tokens
        if custom_headers:
            llm_kwargs["default_headers"] = custom_headers
        llm_kwargs.update(kwargs)
        return ChatOpenAI(**llm_kwargs)
    else:
        logger.debug(f"Creating OpenAI LLM: {model_name}")
        extra = {"max_tokens": max_tokens} if max_tokens is not None else {}
        return ChatOpenAI(
            api_key=SecretStr(api_key),
            model=model_name,
            temperature=temperature,
            streaming=streaming,
            **extra,
            **kwargs,
        )


@otel_trace
def _create_anthropic_llm(
    model_name: str, temperature: float, streaming: bool, custom_headers: Optional[dict] = None, max_tokens: Optional[int] = None, **kwargs
) -> ChatAnthropic:
    """Create Anthropic (Claude) LLM instance with optional LiteLLM gateway support."""
    litellm_config = _get_litellm_config()
    litellm_enabled = litellm_config.get("enabled", False)
    litellm_base_url = litellm_config.get("base_url", "")

    api_key = settings.anthropic_api_key
    litellm_api_key = litellm_config.get("api_key", "")
    if not api_key and not (litellm_enabled and litellm_base_url):
        raise ValueError(
            "ANTHROPIC_API_KEY environment variable is not set. "
            "Please set it in your .env file."
        )

    if litellm_enabled and litellm_base_url:
        logger.debug(f"Creating Anthropic LLM via LiteLLM gateway: {model_name}")
        llm_kwargs = {
            "api_key": SecretStr(litellm_api_key or api_key or "litellm-gateway"),
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "base_url": litellm_base_url,
        }
        if max_tokens is not None:
            llm_kwargs["max_tokens"] = max_tokens
        if custom_headers:
            llm_kwargs["default_headers"] = custom_headers
        llm_kwargs.update(kwargs)
        return ChatOpenAI(**llm_kwargs)
    else:
        logger.debug(f"Creating Anthropic LLM: {model_name}")
        extra = {"max_tokens": max_tokens} if max_tokens is not None else {}
        return ChatAnthropic(
            api_key=SecretStr(api_key),
            model=model_name,
            temperature=temperature,
            streaming=streaming,
            **extra,
            **kwargs,
        )


@otel_trace
def _create_google_llm(
    model_name: str, temperature: float, streaming: bool, custom_headers: Optional[dict] = None, max_tokens: Optional[int] = None, **kwargs
):
    """Create Google (Gemini) LLM instance.

    Priority: Vertex AI via GCP_SERVICE_ACCOUNT_JSON (primary, when configured)
    -> LiteLLM gateway -> direct Gemini Developer API key. When both Vertex AI
    and the LiteLLM gateway are configured, Vertex AI is primary and the
    gateway is wired in as an automatic fallback (via .with_fallbacks()) so a
    Vertex AI outage/misconfig doesn't take the agent down.
    """
    litellm_config = _get_litellm_config()
    litellm_enabled = litellm_config.get("enabled", False)
    litellm_base_url = litellm_config.get("base_url", "")
    litellm_api_key = litellm_config.get("api_key", "")
    api_key = settings.google_api_key

    litellm_llm = None
    if litellm_enabled and litellm_base_url:
        logger.debug(f"Creating Google LLM via LiteLLM gateway: {model_name}")
        llm_kwargs = {
            "api_key": SecretStr(litellm_api_key or api_key or "litellm-gateway"),
            "model": model_name,
            "temperature": temperature,
            "streaming": streaming,
            "base_url": litellm_base_url,
        }
        if max_tokens is not None:
            llm_kwargs["max_tokens"] = max_tokens
        if custom_headers:
            llm_kwargs["default_headers"] = custom_headers
        llm_kwargs.update(kwargs)
        litellm_llm = ChatOpenAI(**llm_kwargs)

    credentials, project_id = _get_vertex_credentials()
    if credentials is not None:
        # Vertex AI takes bare model names ("gemini-1.5-pro"), not the
        # LiteLLM-style "provider/model" string used elsewhere in config.
        vertex_model_name = model_name.split("/", 1)[-1] if "/" in model_name else model_name
        logger.debug(f"Creating Google LLM via Vertex AI (service account): {vertex_model_name}")
        vertex_kwargs = {
            "model": vertex_model_name,
            "temperature": temperature,
            "streaming": streaming,
            "credentials": credentials,
            "project": project_id,
        }
        if max_tokens is not None:
            vertex_kwargs["max_output_tokens"] = max_tokens
        vertex_kwargs.update(kwargs)
        vertex_llm = ChatVertexAI(**vertex_kwargs)
        return vertex_llm.with_fallbacks([litellm_llm]) if litellm_llm is not None else vertex_llm

    if litellm_llm is not None:
        return litellm_llm

    if not api_key:
        raise ValueError(
            "GOOGLE_API_KEY environment variable is not set. "
            "Please set it in your .env file."
        )
    logger.debug(f"Creating Google LLM: {model_name}")
    # Google uses max_output_tokens, not max_tokens
    extra = {"max_output_tokens": max_tokens} if max_tokens is not None else {}
    return ChatGoogleGenerativeAI(
        google_api_key=SecretStr(api_key),
        model=model_name,
        temperature=temperature,
        streaming=streaming,
        **extra,
        **kwargs,
    )


@otel_trace
def get_provider_from_model(model_name: str) -> LLMProvider:
    """
    Infer provider from model name.

    Args:
        model_name: Model name (e.g., "gpt-4o", "claude-3-opus", "gemini-pro")

    Returns:
        Inferred LLM provider

    Raises:
        ValueError: If provider cannot be inferred
    """
    model_lower = model_name.lower()

    # OpenAI models
    if any(prefix in model_lower for prefix in ["gpt-", "o1-", "o3-"]):
        return LLMProvider.OPENAI

    # Anthropic models
    if "claude" in model_lower:
        return LLMProvider.ANTHROPIC

    # Google models
    if "gemini" in model_lower or "palm" in model_lower:
        return LLMProvider.GOOGLE

    raise ValueError(
        f"Cannot infer provider from model name: {model_name}. "
        "Please specify provider explicitly."
    )


@otel_trace
def validate_api_keys() -> dict:
    """
    Validate which API keys are configured.

    Returns:
        Dictionary of provider -> bool (configured or not)
    """
    return {
        "openai": bool(os.getenv("OPENAI_API_KEY")),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY")),
        "google": bool(os.getenv("GOOGLE_API_KEY")),
    }


# Convenience functions for each provider
@otel_trace
def get_openai_llm(model_name: str = "gpt-4o", temperature: float = 0.7, **kwargs):
    """Convenience function to create OpenAI LLM."""
    return get_llm_model(LLMProvider.OPENAI, model_name, temperature, **kwargs)


@otel_trace
def get_anthropic_llm(
    model_name: str = "claude-3-5-sonnet-20241022", temperature: float = 0.7, **kwargs
):
    """Convenience function to create Anthropic LLM."""
    return get_llm_model(LLMProvider.ANTHROPIC, model_name, temperature, **kwargs)


@otel_trace
def get_google_llm(
    model_name: str = "gemini-1.5-pro", temperature: float = 0.7, **kwargs
):
    """Convenience function to create Google LLM."""
    return get_llm_model(LLMProvider.GOOGLE, model_name, temperature, **kwargs)
