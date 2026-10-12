"""
Shared HTTP client for the KYC application's session API.

Leading underscore keeps this module out of tool auto-discovery (registry.py's
discover_tools() skips files starting with "_") — it has no @tool functions of
its own, just the plumbing the kyc_tools.py tools call into.

Auth model: POST /api/auth/login returns an HttpOnly ws_session cookie good for
a fixed 8 hours (not sliding). Rather than tracking that clock, we log in lazily
on first use and re-login once whenever a request comes back 401.
"""

import threading
from typing import Any, Dict

import requests

from src.config.agent_config import get_secrets, load_agent_config
from cams_otel_lib import Logger as logger

DEFAULT_BASE_URL = "http://127.0.0.1:8010/api"


class KycApiError(Exception):
    """Raised for any failure talking to the KYC application's API."""


_session = requests.Session()
_lock = threading.Lock()
_logged_in = False
_config: Dict[str, str] | None = None


def _get_config() -> Dict[str, str]:
    global _config
    if _config is None:
        agent_config = load_agent_config()
        secrets = get_secrets("default", agent_config)
        kyc_api = secrets.get("kyc_api", {})
        _config = {
            "base_url": (kyc_api.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
            "username": kyc_api.get("username", ""),
            "password": kyc_api.get("password", ""),
        }
    return _config


def _safe_detail(resp: requests.Response) -> str:
    try:
        return resp.json().get("detail") or resp.text[:300]
    except Exception:
        return resp.text[:300]


def _login() -> None:
    global _logged_in
    config = _get_config()
    try:
        resp = _session.post(
            f"{config['base_url']}/auth/login",
            json={"username": config["username"], "password": config["password"]},
            timeout=10,
        )
    except requests.exceptions.RequestException as e:
        _logged_in = False
        raise KycApiError(
            f"Could not reach the KYC application at {config['base_url']}: {e}"
        ) from e

    if resp.status_code != 200:
        _logged_in = False
        raise KycApiError(
            f"Login to the KYC application failed ({resp.status_code}): {_safe_detail(resp)}"
        )
    _logged_in = True
    logger.info("Logged in to KYC application API")


def _request(method: str, path: str, **kwargs) -> Dict[str, Any]:
    config = _get_config()
    url = f"{config['base_url']}{path}"

    with _lock:
        if not _logged_in:
            _login()

    def _do() -> requests.Response:
        try:
            return _session.request(method, url, timeout=10, **kwargs)
        except requests.exceptions.RequestException as e:
            raise KycApiError(
                f"Could not reach the KYC application at {config['base_url']}: {e}"
            ) from e

    resp = _do()
    if resp.status_code == 401:
        # ws_session cookie expired (fixed 8h lifetime) — re-login once and retry.
        with _lock:
            _login()
        resp = _do()

    if resp.status_code == 404:
        raise KycApiError(_safe_detail(resp) or f"Not found: {path}")

    if not resp.ok:
        raise KycApiError(
            f"KYC application returned {resp.status_code}: {_safe_detail(resp)}"
        )

    return resp.json()


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


def fetch_kyc_session(session_id: str) -> Dict[str, Any]:
    return _request("GET", f"/kyc-sessions/{session_id}")


def fetch_attention_sessions() -> Dict[str, Any]:
    return _request("GET", "/kyc-sessions", params={"outcome": "attention"})


def search_kyc_sessions(query: str) -> Dict[str, Any]:
    return _request("GET", "/kyc-sessions", params={"q": query})


def search_clients(query: str) -> Dict[str, Any]:
    """The legacy client roster — a separate dataset from kyc-sessions, keyed
    by client_id (e.g. 'CL000044'), not session_id. Matches name, PAN, or
    application_id."""
    return _request("GET", "/applications", params={"q": query})


def fetch_client_profile(client_id: str) -> Dict[str, Any]:
    """Full profile for one legacy client, including linked bank accounts,
    product accounts, documents, screening history, UBOs and risk profile."""
    return _request("GET", f"/clients/{client_id}/kyc-summary")
