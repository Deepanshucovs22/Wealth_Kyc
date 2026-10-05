"""Database connection settings, read from .env (or real environment variables)."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"


def _load_env_file() -> None:
    """Minimal .env reader — real environment variables always win."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env_file()

PG_HOST = os.environ.get("PGHOST", "localhost")
PG_PORT = int(os.environ.get("PGPORT", "5432"))
PG_USER = os.environ.get("PGUSER", "postgres")
PG_PASSWORD = os.environ.get("PGPASSWORD", "")
DB_NAME = os.environ.get("PGDATABASE", "wealth_kyc")
# Only used to issue CREATE DATABASE; never written to.
MAINTENANCE_DB = os.environ.get("PGMAINTENANCE", "postgres")

APP_HOST = os.environ.get("APP_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# Send the session cookie only over HTTPS. Must stay false for plain-http
# localhost, or the browser will never send it back.
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").strip().lower() in {"1", "true", "yes"}


def _dsn(dbname: str) -> str:
    parts = [
        f"host={PG_HOST}",
        f"port={PG_PORT}",
        f"user={PG_USER}",
        f"dbname={dbname}",
        "application_name=covasant-wealth-kyc",
    ]
    if PG_PASSWORD:
        parts.append(f"password={PG_PASSWORD}")
    return " ".join(parts)


def admin_dsn() -> str:
    """Connection to the maintenance database, used only to CREATE DATABASE."""
    return _dsn(MAINTENANCE_DB)


def app_dsn() -> str:
    """Connection to the application database."""
    return _dsn(DB_NAME)


def describe_target() -> str:
    """Human-readable target, with no secrets in it."""
    return f"postgresql://{PG_USER}@{PG_HOST}:{PG_PORT}/{DB_NAME}"
