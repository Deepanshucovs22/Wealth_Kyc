"""
Authentication for the WealthGate console.

Passwords are stored as scrypt derived keys (stdlib, no extra dependency).
Sessions are server-side: the browser holds an opaque random token, the
database holds only its SHA-256, so signing out genuinely revokes access and
a leaked table cannot be replayed as a live session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

# --- tuning ---------------------------------------------------------------
SCRYPT_N = 2 ** 14          # ~16 MB of memory per hash
SCRYPT_R = 8
SCRYPT_P = 1
DK_LEN = 32

SESSION_HOURS = 8           # a working day
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

COOKIE_NAME = "ws_session"


# --- passwords ------------------------------------------------------------
def hash_password(password: str) -> str:
    """Return an encoded scrypt hash: scrypt$n$r$p$<salt>$<key>."""
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=DK_LEN)
    return "$".join([
        "scrypt", str(SCRYPT_N), str(SCRYPT_R), str(SCRYPT_P),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against an encoded hash."""
    try:
        scheme, n, r, p, salt_b64, key_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(key_b64)
        dk = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                            n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except (ValueError, TypeError):
        return False


# --- sessions -------------------------------------------------------------
def new_session_token() -> str:
    """A fresh opaque token for the cookie. Never stored as-is."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def session_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=SESSION_HOURS)


def lockout_until() -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=LOCKOUT_MINUTES)
