"""
Direct PostgreSQL access for the eKYC review tools.

Leading underscore keeps this module out of tool auto-discovery (registry.py's
discover_tools() skips files starting with "_") — it has no @tool functions of
its own, just the connection pool and query helpers kyc_tools.py calls into.

Replaces the previous HTTP client (_kyc_client.py): the agent now reads the
KYC database directly rather than going through the application's REST API.
Two schemas are in play:
  kyc_intake — new-account OCR intake (sessions, form data, OCR results)
  kyc        — the legacy onboarded-client roster (clients, bank accounts, …)

Connection settings come from agent_config.json → secrets.database.postgres.
"""

import asyncio
from typing import Any, Dict, List, Optional

import asyncpg

from src.config.agent_config import get_secrets, load_agent_config
from cams_otel_lib import Logger as logger

# Bounds every query so an unreachable/slow database surfaces as a clear error
# instead of hanging the agent loop (an LLM call has no timeout of its own, so
# a stuck query would otherwise stall the whole request indefinitely).
CONNECT_TIMEOUT_SECONDS = 10
QUERY_TIMEOUT_SECONDS = 15


class KycDbError(Exception):
    """Raised for any failure talking to the KYC database."""


_pool: Optional[asyncpg.Pool] = None
_pool_lock = asyncio.Lock()


def _get_db_config() -> Dict[str, Any]:
    """Read secrets.database.postgres out of agent_config.json."""
    secrets = get_secrets("default", load_agent_config())
    db = (secrets.get("database") or {}).get("postgres") or {}
    if not db.get("host") or not db.get("database"):
        raise KycDbError(
            "No database configuration found — expected "
            "agent_config.json → secrets.database.postgres with host/database."
        )
    return db


async def _get_pool() -> asyncpg.Pool:
    """Create the connection pool on first use, then reuse it."""
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                cfg = _get_db_config()
                try:
                    _pool = await asyncpg.create_pool(
                        host=cfg["host"],
                        port=int(cfg.get("port", 5432)),
                        user=cfg.get("user"),
                        password=cfg.get("password"),
                        database=cfg["database"],
                        min_size=1,
                        max_size=5,
                        timeout=CONNECT_TIMEOUT_SECONDS,
                        command_timeout=QUERY_TIMEOUT_SECONDS,
                    )
                except Exception as e:
                    raise KycDbError(
                        f"Could not connect to the KYC database at "
                        f"{cfg['host']}:{cfg.get('port', 5432)}/{cfg['database']}: {e}"
                    ) from e
                logger.info("Connected to KYC database")
    return _pool


async def fetch_all(query: str, *args) -> List[Dict[str, Any]]:
    """Run a SELECT and return rows as plain dicts (JSON-serialisable)."""
    pool = await _get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(query, *args, timeout=QUERY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as e:
        raise KycDbError(
            f"The KYC database did not respond within {QUERY_TIMEOUT_SECONDS}s."
        ) from e
    except Exception as e:
        raise KycDbError(f"KYC database query failed: {e}") from e
    return [_jsonable(dict(r)) for r in rows]


async def fetch_one(query: str, *args) -> Optional[Dict[str, Any]]:
    """Run a SELECT expected to match at most one row."""
    rows = await fetch_all(query, *args)
    return rows[0] if rows else None


def _jsonable(row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert driver-native types (date, Decimal, …) to JSON-friendly ones.

    jsonb columns already come back as parsed Python objects, so they pass
    through untouched.
    """
    out: Dict[str, Any] = {}
    for key, value in row.items():
        if value is None or isinstance(value, (str, int, float, bool, list, dict)):
            out[key] = value
        else:
            out[key] = str(value)
    return out


def is_valid_session_id(session_id: str) -> bool:
    """Loose shape check for 'KYC-YYYYMMDD-XXXXXX' — avoids a round trip for obvious typos."""
    parts = session_id.split("-")
    return (
        len(parts) == 3
        and parts[0] == "KYC"
        and len(parts[1]) == 8
        and parts[1].isdigit()
        and len(parts[2]) == 6
    )
