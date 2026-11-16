"""Console APIs for connectors and the OAuth callback (docs/ui-spec.md §3.4, design §7.3).

All ``/api/connectors…`` routes are owner-only (bearer or the console cookie,
writes with ``X-ANX: 1``). Responses never contain a secret value.

``GET /oauth/connectors/callback/<name>`` finishes a login: the one-time
``state`` (5 minutes) must match a login the console started, and the
request must carry the owner session. The console cookie is SameSite=Strict,
which a browser does not send on the cross-site redirect back from the
provider; the callback therefore answers such a request with a same-site
bounce (one meta refresh to itself), after which the cookie is present.
"""
from __future__ import annotations

import html
import logging
from urllib.parse import quote, urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from ..inbox_web import NO_STORE
from .registry import ConnectorError

log = logging.getLogger(__name__)
CALLBACK_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'"


class ConnectorsWeb:
    def __init__(self, host, console):
        self.host = host
        self.console = console

    @staticmethod
    def _json(data, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers=NO_STORE)

    def _error(self, exc: ConnectorError) -> JSONResponse:
        return self._json(exc.body(), exc.status)

    async def _guard(self, request: Request):
        if await self.console.owner(request) is None:
            return self.console.unauthorized()
        return None

    async def collection(self, request: Request) -> Response:
        if (denied := await self._guard(request)) is not None:
            return denied
        if request.method == "GET":
            return self._json({"connectors": self.host.list_views()})
        body = await self.console._body(request)
        try:
            return self._json({"connector": await self.host.create(body)}, 201)
        except ConnectorError as exc:
            return self._error(exc)

    async def item(self, request: Request) -> Response:
        if (denied := await self._guard(request)) is not None:
            return denied
        name = request.path_params["name"]
        try:
            if request.method == "GET":
                return self._json({"connector": self.host.view(self.host._entry(name))})
            if request.method == "DELETE":
                await self.host.delete(name)
                return Response(status_code=204, headers=NO_STORE)
            body = await self.console._body(request)
            return self._json({"connector": await self.host.update(name, body)})
        except ConnectorError as exc:
            return self._error(exc)

    async def action(self, request: Request) -> Response:
        if (denied := await self._guard(request)) is not None:
            return denied
        name, action = request.path_params["name"], request.path_params["action"]
        try:
            if action == "test":
                return self._json(await self.host.test(name))
            if action == "login":
                return self._json(await self.host.login(name, origin=self.console.origin(request)))
            if action in ("logout", "token"):
                return self._json({"connector": await self.host.logout(name)})
        except ConnectorError as exc:
            return self._error(exc)
        return self._json({"error": "unknown action"}, 404)

    async def token(self, request: Request) -> Response:
        """``DELETE /api/connectors/<name>/token`` (design §7.3 step 7) = logout."""
        request.path_params["action"] = "token"
        return await self.action(request)

    async def probe(self, request: Request) -> Response:
        if (denied := await self._guard(request)) is not None:
            return denied
        try:
            return self._json(await self.host.probe(await self.console._body(request)))
        except ConnectorError as exc:
            return self._error(exc)

    async def export(self, request: Request) -> Response:
        if (denied := await self._guard(request)) is not None:
            return denied
        return self._json(self.host.export())

    async def import_(self, request: Request) -> Response:
        if (denied := await self._guard(request)) is not None:
            return denied
        try:
            return self._json(await self.host.import_(await self.console._body(request)))
        except ConnectorError as exc:
            return self._error(exc)

    # -- the browser comes back from the provider ------------------------------------------
    @staticmethod
    def _page(title: str, text: str, *, status: int = 200, link: tuple[str, str] | None = None,
              refresh: str | None = None) -> HTMLResponse:
        meta = f'<meta http-equiv="refresh" content="0;url={html.escape(refresh, quote=True)}">' if refresh else ""
        anchor = f'<p><a href="{html.escape(link[0], quote=True)}">{html.escape(link[1])}</a></p>' if link else ""
        body = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{meta}<title>{html.escape(title)} · Agent Nexus</title>
<style>body{{font:16px/1.6 system-ui,sans-serif;max-width:520px;margin:12vh auto;padding:0 20px;color:#1d2433;background:#f7f8fa}}
@media (prefers-color-scheme:dark){{body{{color:#e6e9ef;background:#12151b}}a{{color:#8fb3ff}}}}h1{{font-size:20px}}</style></head>
<body><h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>{anchor}</body></html>"""
        return HTMLResponse(body, status_code=status, headers={**NO_STORE, "Content-Security-Policy": CALLBACK_CSP})

    @staticmethod
    def _back(name: str, result: str, reason: str | None = None) -> RedirectResponse:
        query = {"login": name, "result": result}
        if reason:
            query["reason"] = reason
        return RedirectResponse(f"/#/connectors?{urlencode(query)}", status_code=303, headers=NO_STORE)

    async def callback(self, request: Request) -> Response:
        name = request.path_params["name"]
        params = request.query_params
        if params.get("error"):
            # The provider refused (or the owner pressed "deny"): back to the console.
            return self._back(name, "error", params.get("error")[:64])
        code, state = params.get("code"), params.get("state")
        if not code or not state:
            return self._page("登录未完成", "回调缺少 code 或 state。回到控制台重新点“登录”。", status=400,
                              link=("/#/connectors", "回到连接器"))
        try:
            self.host.oauth.check_state(name, state)
        except ConnectorError as exc:
            return self._page("登录未完成", exc.message, status=400, link=("/#/connectors", "回到连接器"))
        if await self.console.owner(request) is None:
            if params.get("b") != "1":
                # Same-site bounce so the SameSite=Strict console cookie is sent.
                target = f"{request.url.path}?{request.url.query}&b=1"
                return self._page("正在完成登录…", "正在返回 Agent Nexus 控制台。", refresh=target)
            next_path = f"{request.url.path}?{request.url.query}"
            return self._page("请先登录控制台", "完成连接器登录需要网关控制台的 owner 会话。登录后会继续这个连接器的授权。",
                              status=403, link=(f"/auth/login?next={quote(next_path)}", "登录控制台"))
        try:
            view = await self.host.finish_login(name, code=code, state=state)
        except ConnectorError as exc:
            log.warning("connector %s: login callback failed (%s)", name, exc.code)
            return self._back(name, "error", exc.code)
        return self._back(name, "ok" if view["state"] == "ok" else "error",
                          None if view["state"] == "ok" else view["state"])

    def routes(self) -> list[Route]:
        return [
            Route("/api/connectors", self.collection, methods=["GET", "POST"]),
            Route("/api/connectors/export", self.export, methods=["GET"]),
            Route("/api/connectors/import", self.import_, methods=["PUT", "POST"]),
            Route("/api/connectors/probe", self.probe, methods=["POST"]),
            Route("/api/connectors/{name}", self.item, methods=["GET", "PUT", "DELETE"]),
            Route("/api/connectors/{name}/token", self.token, methods=["DELETE"]),
            Route("/api/connectors/{name}/{action}", self.action, methods=["POST"]),
            Route("/oauth/connectors/callback/{name}", self.callback, methods=["GET"]),
        ]
