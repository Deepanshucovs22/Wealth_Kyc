"""
Topic-based RBAC for chat requests: only admin roles may discuss "Covasant".

This sits on top of platform_authlib's AuthEnforcementMiddleware (Layer 1),
which verifies the caller's JWT and attaches the real, verified
`request.state.user` (an `AuthenticatedUser` with `.grants`) before any route
handler runs — see src/agent/__main__.py's middleware wiring. This module is
this agent's own, narrower Layer 2 check on top of that: even a fully
authenticated caller with a valid token may not chat about "Covasant" unless
one of their granted roles is an admin role. Every other topic is open to
any authenticated caller regardless of role.
"""

import re
from typing import List

from fastapi import Request

# Role strings must match exactly what auth-service mints into JWT grants
# (see platform_authlib.models.Grant.role) — this agent never invents its
# own role vocabulary, it just recognises the two admin-tier role names.
ADMIN_ROLES = {"admin", "System Administrator"}

RESTRICTED_TOPIC = "covasant"
_RESTRICTED_TOPIC_PATTERN = re.compile(re.escape(RESTRICTED_TOPIC), re.IGNORECASE)


def mentions_restricted_topic(text: str) -> bool:
    """True if *text* references the topic this agent restricts to admins."""
    return bool(_RESTRICTED_TOPIC_PATTERN.search(text or ""))


def get_caller_roles(request: Request) -> List[str]:
    """
    Roles granted to the caller of *request*.

    Preferred source: `request.state.user` — the verified `AuthenticatedUser`
    platform_authlib's `AuthEnforcementMiddleware` attaches for every
    non-bypassed request, sourced from the caller's signature-verified JWT.
    Falls back to an `X-User-Role` header only when `request.state.user` is
    absent (e.g. a bare test client built without the real middleware stack)
    — never trusted over verified grants when both are present.
    """
    user = getattr(getattr(request, "state", None), "user", None)
    grants = getattr(user, "grants", None) if user is not None else None
    if grants is not None:
        return [grant.role for grant in grants]

    header_role = request.headers.get("X-User-Role")
    return [header_role] if header_role else []


def is_admin(request: Request) -> bool:
    """True if the caller holds one of ADMIN_ROLES."""
    return any(role in ADMIN_ROLES for role in get_caller_roles(request))
