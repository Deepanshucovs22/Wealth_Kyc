#!/bin/bash
set -e

echo "==================================="
echo "Agent Docker Container Starting"
echo "==================================="

# Function to inject config from environment or mounted volume.
#
# IMPORTANT: this must NEVER write to /app/src/config/agent_config.json (the baked-in
# image config). That file is the code-fallback layer src/config/agent_config.py merges
# CAMS-mounted config on top of (load_agent_config() → CAMSConfigAdapter's
# _deep_fill_missing) — overwriting it destroys the defaults a partial CAMS config
# (e.g. a cams_v2 export missing secrets/tools) would otherwise fall back to, silently
# dropping litellm/db/tool config. Any CAMS-provided config is delivered to the
# SEPARATE external path (/app/config/agent_config.json) with APP_CONFIG_PATH pointed
# at it instead, leaving the baked-in file untouched.
inject_config() {
    echo "Checking for configuration injection..."

    EXTERNAL_CONFIG_PATH="/app/config/agent_config.json"

    # Priority 1: APP_CONFIG_PATH already set by the platform — respect it as-is,
    # baked-in file remains the fallback layer.
    if [ ! -z "$APP_CONFIG_PATH" ]; then
        echo "APP_CONFIG_PATH is set to: $APP_CONFIG_PATH"
        if [ -f "$APP_CONFIG_PATH" ]; then
            echo "✓ External config found at $APP_CONFIG_PATH"
            echo "✓ Agent will load config from external path, merged over baked-in defaults"
        else
            echo "⚠ APP_CONFIG_PATH set but file not found at $APP_CONFIG_PATH"
            echo "⚠ Will fall back to baked-in config"
        fi
        return
    fi

    # Priority 2: CONFIG_JSON provided as an environment variable — write it to the
    # SEPARATE external path and point APP_CONFIG_PATH there (never the baked-in file).
    if [ ! -z "$CONFIG_JSON" ]; then
        echo "Injecting config from CONFIG_JSON environment variable to external path..."
        echo "$CONFIG_JSON" > "$EXTERNAL_CONFIG_PATH"
        export APP_CONFIG_PATH="$EXTERNAL_CONFIG_PATH"
        echo "✓ Config injected from environment variable to $EXTERNAL_CONFIG_PATH"
        echo "✓ APP_CONFIG_PATH set to $EXTERNAL_CONFIG_PATH; baked-in config remains the fallback layer"
        return
    fi

    # Priority 3: config file already mounted at the external path — point
    # APP_CONFIG_PATH at it directly (no copy needed, works even if mounted read-only).
    if [ -f "$EXTERNAL_CONFIG_PATH" ]; then
        export APP_CONFIG_PATH="$EXTERNAL_CONFIG_PATH"
        echo "✓ Found config mounted at $EXTERNAL_CONFIG_PATH"
        echo "✓ APP_CONFIG_PATH set to $EXTERNAL_CONFIG_PATH; baked-in config remains the fallback layer"
        return
    fi

    # No external config delivered by any mechanism — use the baked-in image config as-is.
    if [ -f "/app/src/config/agent_config.json" ]; then
        echo "✓ No external config found — using baked-in agent_config.json from image"
    else
        echo "⚠ No agent_config.json found (external or baked-in), using defaults"
    fi
}

# Function to display configuration summary
show_config() {
    echo ""
    echo "Configuration Summary:"
    echo "---------------------"
    echo "Agent Name: ${AGENT_NAME:-agent}"
    echo "Host: ${HOST:-0.0.0.0}"
    echo "Port: ${PORT:-9999}"
    echo "Log Level: ${LOG_LEVEL:-INFO}"
    echo "Streaming: ${STREAMING_ENABLED:-true}"
    echo "Multi-Tenant: ${MULTI_TENANT_ENABLED:-true}"
    echo "Langfuse: ${LANGFUSE_ENABLED:-false}"
    echo "LiteLLM Gateway: ${USE_LITELLM_GATEWAY:-false}"
    
    if [ ! -z "$APP_CONFIG_PATH" ]; then
        echo "Config Path: $APP_CONFIG_PATH (external)"
    else
        echo "Config Path: /app/src/config/agent_config.json (internal)"
    fi
    
    if [ ! -z "$ENV_ID" ]; then
        echo "Environment ID: $ENV_ID"
    fi
    
    if [ ! -z "$TENANT_IDS" ]; then
        echo "Tenant IDs: $TENANT_IDS"
    fi
    
    echo "---------------------"
    echo ""
}

# Inject configuration
inject_config

# Show configuration summary
show_config

# Execute the main command
echo "Starting agent..."
echo "==================================="
exec "$@"
