"""Who may approve or reject a remediation.

The write endpoints are the human half of human-in-the-loop, so the server has to
know which human it is talking to. Two modes, chosen by configuration:

- **Token mode.** ``SENTINEL_APPROVER_TOKENS`` holds ``name:token`` pairs. A write
  needs ``Authorization: Bearer <token>``, and the approver recorded on the
  incident is the *name the token belongs to* - never a name the client sent.
- **Local mode.** With no tokens configured, writes are accepted only from the
  loopback interface. That keeps ``sentinel serve`` on a laptop zero-config while
  a deployment that forgot to configure tokens fails closed instead of open.

The tokens are read from the environment at call time rather than held on the
settings object, for the same reason as the Anthropic key: a secret should never
end up in a serialised settings object, a log line or an incident document.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from sentinel.logging import get_logger

log = get_logger(__name__)

APPROVER_TOKENS_ENV = "SENTINEL_APPROVER_TOKENS"

#: Shorter tokens are refused outright: a guessable token is worse than none,
#: because it looks like protection.
MIN_TOKEN_LENGTH = 16

_bearer = HTTPBearer(auto_error=False, description="An approver token.")


def parse_approver_tokens(raw: str) -> dict[str, str]:
    """Parse ``name:token,name:token`` into a token -> approver-name mapping.

    Raises :class:`ValueError` on anything malformed. A half-understood access
    list must not be half-applied.
    """
    tokens: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, separator, token = entry.partition(":")
        name, token = name.strip(), token.strip()
        if not separator or not name or not token:
            raise ValueError(f"{APPROVER_TOKENS_ENV} entries must look like 'name:token'")
        if len(token) < MIN_TOKEN_LENGTH:
            raise ValueError(
                f"{APPROVER_TOKENS_ENV}: token for {name!r} is shorter than "
                f"{MIN_TOKEN_LENGTH} characters"
            )
        if token in tokens:
            raise ValueError(f"{APPROVER_TOKENS_ENV}: the same token is assigned twice")
        tokens[token] = name
    return tokens


def configured_approver_tokens() -> dict[str, str]:
    """The token -> approver mapping from the environment (empty when unset)."""
    return parse_approver_tokens(os.environ.get(APPROVER_TOKENS_ENV, ""))


def _is_loopback(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        return ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _match(presented: str, tokens: dict[str, str]) -> str | None:
    """Constant-time lookup: every configured token is compared, every time."""
    matched: str | None = None
    for token, name in tokens.items():
        if hmac.compare_digest(presented.encode(), token.encode()):
            matched = name
    return matched


async def authenticated_approver(request: Request) -> str | None:
    """FastAPI dependency guarding the write endpoints.

    Returns the approver's name in token mode, or ``None`` in local mode (the
    caller is on the loopback interface and no tokens are configured). Raises
    401/403/503 otherwise.
    """
    try:
        tokens = configured_approver_tokens()
    except ValueError as error:
        log.error("auth.misconfigured", error=str(error))
        raise HTTPException(
            status_code=503, detail="Approver tokens are misconfigured; approvals are disabled."
        ) from error

    if not tokens:
        if _is_loopback(request):
            return None
        raise HTTPException(
            status_code=403,
            detail=(
                "Approvals from non-local clients are disabled until "
                f"{APPROVER_TOKENS_ENV} is configured."
            ),
        )

    credentials: HTTPAuthorizationCredentials | None = await _bearer(request)
    approver = _match(credentials.credentials, tokens) if credentials else None
    if approver is None:
        raise HTTPException(
            status_code=401,
            detail="A valid approver token is required.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return approver
