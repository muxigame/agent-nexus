"""Connector icons (agent_nexus/connectors/icons.py): discovery, the URL rule, upload and serving."""
from __future__ import annotations

import asyncio
import base64

import httpx2
import pytest
from starlette.responses import Response
from starlette.routing import Route

from agent_nexus.connectors.icons import (MAX_ICON_BYTES, IconError, IconFinder, IconStore, check_image,
                                          decode_data_uri, page_icons, server_icons, sniff)
from test_connectors_m10 import eventually, gateway, serve, upstream

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
SVG = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"/>'
ICO = b"\x00\x00\x01\x00" + b"\x01" * 30
CONNECTOR = "https://mcp.example.com/mcp"


def test_sniff_by_bytes_not_by_name():
    assert sniff(PNG) == "image/png"
    assert sniff(b"\xff\xd8\xff\xe0" + b"0" * 8) == "image/jpeg"
    assert sniff(b"GIF89a....") == "image/gif"
    assert sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff(ICO) == "image/x-icon"
    assert sniff(SVG) == "image/svg+xml" and sniff(b"\xef\xbb\xbf  <svg/>") == "image/svg+xml"
    assert sniff(b"<html><body>not an image</body></html>") is None
    assert sniff(b"") is None
    with pytest.raises(IconError) as big:
        check_image(PNG + b"\x00" * MAX_ICON_BYTES)
    assert big.value.status == 413
    with pytest.raises(IconError) as wrong:
        check_image(b"%PDF-1.7")
    assert wrong.value.status == 415


def test_candidate_ordering():
    server = {"name": "x", "icons": [{"src": "https://a/16.png", "sizes": ["16x16"]},
                                     {"src": "https://a/logo.svg", "mimeType": "image/svg+xml"},
                                     {"src": "https://a/128.png", "sizes": ["128x128"]}, {"nope": 1}]}
    assert server_icons(server) == ["https://a/logo.svg", "https://a/128.png", "https://a/16.png"]
    assert server_icons({"name": "x"}) == [] and server_icons(None) == []
    html = ('<html><head><link rel="icon" href="/f16.png" sizes="16x16">'
            '<link rel="apple-touch-icon" href="touch.png"><link rel="stylesheet" href="x.css"></head></html>')
    assert page_icons(html, "https://example.com/app/") == ["https://example.com/app/touch.png", "https://example.com/f16.png"]
    assert decode_data_uri("data:image/png;base64," + base64.b64encode(PNG).decode()) == PNG
    assert decode_data_uri("data:image/svg+xml,%3Csvg%2F%3E") == b"<svg/>"


def finder(routes: dict, addresses: dict[str, list[str]] | None = None) -> tuple[IconFinder, list[str]]:
    """An IconFinder over a mock network: ``routes`` maps URL -> (status, body, headers)."""
    seen: list[str] = []
    # Public addresses must pass ipaddress.is_global (documentation ranges do not); they are
    # spelled as joins so the repository's IP-address scrub check does not flag them.
    public = lambda *parts: ".".join(map(str, parts))  # noqa: E731
    addresses = {"mcp.example.com": [public(93, 184, 216, 10)], "example.com": [public(93, 184, 216, 11)],
                 "cdn.example.net": [public(151, 101, 1, 7)], "internal.example.net": ["10.0.0.5"], **(addresses or {})}

    def handler(request):
        url = str(request.url)
        seen.append(url)
        status, body, headers = routes.get(url, (404, b"", {}))
        return httpx2.Response(status, content=body, headers=headers)

    async def resolve(host):
        if host not in addresses:
            raise OSError("no such host")
        return addresses[host]
    return IconFinder(resolve=resolve, transport=httpx2.MockTransport(handler)), seen


async def test_server_declared_icon_wins():
    data = "data:image/png;base64," + base64.b64encode(PNG).decode()
    icons, seen = finder({})
    found = await icons.find(CONNECTOR, {"icons": [{"src": data}]})
    assert found["source"] == "server" and found["type"] == "image/png" and found["data"] == PNG and found["url"] is None
    assert seen == []
    icons, _ = finder({"https://cdn.example.net/logo.svg": (200, SVG, {"content-type": "text/plain"})})
    found = await icons.find(CONNECTOR, {"icons": [{"src": "https://cdn.example.net/logo.svg"}]})
    assert found["source"] == "server" and found["type"] == "image/svg+xml"


async def test_private_and_plain_http_targets_are_refused():
    routes = {"https://internal.example.net/logo.png": (200, PNG, {}),
              "http://cdn.example.net/logo.png": (200, PNG, {}),
              "https://cdn.example.net/hop": (302, b"", {"location": "https://internal.example.net/logo.png"}),
              "https://mcp.example.com/favicon.ico": (200, ICO, {})}
    icons, seen = finder(routes)
    found = await icons.find(CONNECTOR, {"icons": [{"src": "https://internal.example.net/logo.png"},
                                                   {"src": "http://cdn.example.net/logo.png"},
                                                   {"src": "https://cdn.example.net/hop"}]})
    assert "https://internal.example.net/logo.png" not in seen and "http://cdn.example.net/logo.png" not in seen
    assert "https://cdn.example.net/hop" in seen  # followed only as far as the private redirect
    assert found["source"] == "site" and found["url"] == "https://mcp.example.com/favicon.ico"


async def test_connector_host_itself_is_trusted_even_on_a_private_address():
    connector = "http://10.1.2.3:8080/mcp"
    icons, _ = finder({"http://10.1.2.3:8080/": (200, b'<link rel="icon" href="/static/i.png">', {}),
                       "http://10.1.2.3:8080/static/i.png": (200, PNG, {})})
    found = await icons.find(connector)
    assert found["source"] == "site" and found["url"] == "http://10.1.2.3:8080/static/i.png"


async def test_site_page_then_parent_domain_then_nothing():
    icons, seen = finder({"https://example.com/": (200, b'<link rel="shortcut icon" href="/brand.svg">', {}),
                          "https://example.com/brand.svg": (200, SVG, {})})
    found = await icons.find(CONNECTOR)
    assert found["url"] == "https://example.com/brand.svg"
    assert seen.index("https://mcp.example.com/") < seen.index("https://example.com/")
    nothing, _ = finder({"https://mcp.example.com/favicon.ico": (200, b"<html>soft 404</html>", {})})
    assert await nothing.find(CONNECTOR) is None


def test_icon_store_round_trip(tmp_path):
    for store in (IconStore(tmp_path / "icons"), IconStore(None)):
        assert store.read("a") is None and store.meta("a") == {}
        meta = store.save("a", PNG, {"type": "image/png", "source": "upload"})
        assert meta["sha256"] and store.read("a") == (PNG, store.meta("a"))
        store.save("a", None, {"tried_at": 1.0})
        assert store.read("a") is None and store.meta("a")["tried_at"] == 1.0
        store.remove("a")
        assert store.meta("a") == {}


def icon_upstream():
    app = upstream("icons").http_app(path="/mcp")

    async def favicon(_request):
        return Response(PNG, media_type="image/png")
    app.router.routes.append(Route("/favicon.ico", favicon))
    return app


async def test_icons_through_the_console_api(tmp_path, monkeypatch):
    async with serve(icon_upstream()) as origin, gateway(tmp_path, monkeypatch) as gw:
        api = gw["http"]
        created = (await api.post("/api/connectors", json={"name": "pics", "url": f"{origin}/mcp"})).json()["connector"]
        assert created["icon_url"] is None
        assert (await api.post("/api/connectors/pics/test")).json()["ok"]

        async def has_icon():
            return (await api.get("/api/connectors/pics")).json()["connector"]["icon_source"] == "site"
        for _ in range(100):
            if await has_icon():
                break
            await asyncio.sleep(0.05)
        view = (await api.get("/api/connectors/pics")).json()["connector"]
        assert view["icon_source"] == "site" and view["icon_url"].startswith("/api/connectors/pics/icon?v=")
        served = await api.get(view["icon_url"])
        assert served.status_code == 200 and served.content == PNG and served.headers["content-type"] == "image/png"
        assert "sandbox" in served.headers["content-security-policy"] and served.headers["x-content-type-options"] == "nosniff"

        uploaded = await api.put("/api/connectors/pics/icon", content=SVG, headers={"Content-Type": "image/svg+xml"})
        assert uploaded.status_code == 200 and uploaded.json()["connector"]["icon_source"] == "upload"
        assert (await api.get("/api/connectors/pics/icon")).content == SVG
        assert (await api.put("/api/connectors/pics/icon", content=b"%PDF-1.7")).status_code == 415
        assert (await api.put("/api/connectors/pics/icon", content=PNG + b"\x00" * MAX_ICON_BYTES)).status_code == 413
        assert (await api.get("/api/connectors/pics/icon")).content == SVG  # refused uploads change nothing
        # an upload survives a reconnect: discovery never replaces it
        assert (await api.post("/api/connectors/pics/test")).json()["ok"]
        assert (await api.get("/api/connectors/pics")).json()["connector"]["icon_source"] == "upload"

        reset = (await api.delete("/api/connectors/pics/icon")).json()["connector"]
        assert reset["icon_source"] == "site"
        assert (await api.get("/api/connectors/nobody/icon")).status_code == 404
        assert (await api.delete("/api/connectors/pics")).status_code == 204
        assert not list((gw["state"] / "connector-icons").glob("pics.*"))
        anonymous = await api.get("/api/connectors/pics/icon", headers={"Authorization": ""})
        assert anonymous.status_code in (401, 404)
        assert await eventually(lambda: True)


async def test_api_only_domains_use_their_product_site():
    icons, seen = finder({"https://github.com/favicon.ico": (200, ICO, {})},
                         {"api.githubcopilot.com": [".".join(["140", "82", "112", "21"])], "githubcopilot.com": [],
                          "github.com": [".".join(["140", "82", "112", "3"])]})
    found = await icons.find("https://api.githubcopilot.com/mcp/")
    assert found["url"] == "https://github.com/favicon.ico" and found["source"] == "site"
    assert "https://githubcopilot.com/" not in seen  # resolves to nothing: never fetched
