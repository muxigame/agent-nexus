"""The one place a service's ``auth`` config becomes a FastMCP auth provider.

========  ===================================================================
``none``    no authentication; :mod:`mcpkit.service` then refuses to listen
            on anything but loopback.
``bearer``  static tokens from ``bearer_secrets`` (local clients, scripts).
``oauth``   :class:`~mcpkit.oauth.PasswordOAuth` for remote clients such as
            ChatGPT, with ``bearer_secrets`` still accepted beside it through
            FastMCP's ``MultiAuth``.
========  ===================================================================
"""
from __future__ import annotations

import hashlib
import hmac

from fastmcp.server.auth import AccessToken, AuthProvider, MultiAuth, TokenVerifier

from .config import ConfigError, ServiceConfig, protect, read_secret


class StaticBearer(TokenVerifier):
    """Accepts any of a fixed set of tokens, compared as digests in constant time."""

    def __init__(self, tokens: list[str], scopes: list[str], *, required_scopes: list[str] | None = None,
                 subject: str = "owner"):
        super().__init__(required_scopes=required_scopes)
        self._digests = [hashlib.sha256(t.encode("utf-8")).hexdigest() for t in tokens]
        self._scopes = list(scopes)
        self._subject = subject

    async def verify_token(self, token: str) -> AccessToken | None:
        candidate = hashlib.sha256(token.encode("utf-8")).hexdigest()
        # Compare against every digest so timing does not reveal which matched.
        matched = False
        for expected in self._digests:
            matched |= hmac.compare_digest(candidate, expected)
        if not matched:
            return None
        return AccessToken(token=token, client_id="local-bearer", scopes=self._scopes, subject=self._subject)


def build_auth(config: ServiceConfig) -> AuthProvider | None:
    auth = config.auth
    if auth.mode == "none":
        return None
    tokens = [read_secret(config, name) for name in auth.bearer_secrets]
    if auth.mode == "bearer":
        # Standalone verifier: it enforces the required scopes itself.
        return StaticBearer(tokens, auth.scopes, required_scopes=auth.required_scopes, subject=auth.subject)
    # Beside OAuth, MultiAuth enforces the server's required scopes.
    bearer = StaticBearer(tokens, auth.scopes, subject=auth.subject) if tokens else None

    from .oauth import PasswordOAuth  # imports the SDK's OAuth server stack; keep bearer-only services light

    store = config.oauth_store
    try:
        provider = PasswordOAuth(
            name=config.name, origin=config.public_url or "", mcp_path=config.path,
            password=read_secret(config, auth.password_secret or ""), store=store, auth=auth,
        )
    except OSError as exc:
        raise ConfigError(f"OAuth store {store} cannot be opened: {exc.strerror or exc}") from None
    if store.is_relative_to(config.secrets_dir):
        protect(store)
    return MultiAuth(server=provider, verifiers=[bearer]) if bearer else provider
