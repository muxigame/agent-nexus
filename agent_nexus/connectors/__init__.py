"""Third-party MCP connectors hosted on the gateway (design §7, decision D10; M10, M11).

* :mod:`.registry` — the single registry (``connectors`` table in
  ``gateway.sqlite3``), input validation, and the write-only secret store under
  ``<state_dir>/connectors/<name>/``.
* :mod:`.oauth` — the OAuth 2.1 client for connectors (RFC 9728 / 8414 discovery,
  RFC 7591 dynamic registration, PKCE S256, RFC 8707 ``resource``, refresh).
* :mod:`.host` — ``ConnectorHost``: one shared upstream MCP session per
  connector, exposed as ``hub/<name>`` to every client through
  mcplist / mcpdetail / toolsearch / tooluse, with no device online.
* :mod:`.web` — the console APIs (``/api/connectors…``) and the OAuth callback
  ``/oauth/connectors/callback/<name>``.
"""
from .host import ConnectorHost, HUB_ID
from .registry import ConnectorError

__all__ = ["ConnectorHost", "ConnectorError", "HUB_ID"]
