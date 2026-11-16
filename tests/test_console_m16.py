"""The gateway web console (docs/ui-spec.md §2-§3): login, session cookie, console APIs, downloads."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent_nexus.cloud import build_cloud_app, config_warnings

ORIGIN = "https://hub.example.test"
SERVER_TOKEN = "s" * 40
PASSWORD = "p" * 40
STATIC = Path(__file__).resolve().parents[1] / "agent_nexus" / "static"


def oauth_config(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", "a" * 40)
    secrets_dir = tmp_path / "secrets" / "workspace"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "oauth-password").write_text(PASSWORD)
    return {"agents": {"pc-main": "AGENT_TOKEN"}, "public_url": ORIGIN, "state_dir": str(tmp_path / "state"),
            "auth": {"mode": "oauth", "password_secret": "workspace/oauth-password", "scopes": ["tools"],
                     "required_scopes": ["tools"], "store": str(tmp_path / "state" / "oauth.sqlite3")}, **extra}


def bearer_config(tmp_path, monkeypatch, **extra):
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", "a" * 40)
    return {"token_env": "SERVER_TOKEN", "agents": {"pc-main": "AGENT_TOKEN"}, "public_url": ORIGIN,
            "state_dir": str(tmp_path / "state"), **extra}


def login(client, password=PASSWORD, *, remember=True, next_path="/"):
    data = {"password": password, "next": next_path}
    if remember:
        data["remember"] = "1"
    return client.post("/auth/login", data=data, headers={"Origin": ORIGIN}, follow_redirects=False)


def test_password_login_sets_the_console_session_cookie(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(oauth_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 302 and root.headers["location"] == "/auth/login"
        assert client.get("/api/console/summary").status_code == 401
        page = client.get("/auth/login")
        assert page.status_code == 200 and 'name="password"' in page.text and "/ui/ui.css" in page.text
        assert "default-src 'self'" in page.headers["content-security-policy"]
        wrong = login(client, "nope")
        assert wrong.status_code == 401 and "密码不正确" in wrong.text and "set-cookie" not in wrong.headers
        # a foreign Origin is refused outright
        assert client.post("/auth/login", data={"password": PASSWORD}, headers={"Origin": "https://evil.test"}).status_code == 403
        ok = login(client, next_path="/#/devices")
        assert ok.status_code == 303 and ok.headers["location"] == "/#/devices"
        cookie = ok.headers["set-cookie"]
        lowered = cookie.lower()
        assert cookie.startswith("__Host-anx_inbox=") and "httponly" in lowered and "secure" in lowered
        assert "samesite=strict" in lowered and "path=/" in lowered and "max-age=2592000" in lowered
        console = client.get("/")
        assert console.status_code == 200 and "/console/app.js" in console.text and "/ui/tokens.css" in console.text
        summary = client.get("/api/console/summary").json()
        assert summary["gateway"]["mcp_url"] == f"{ORIGIN}/mcp" and summary["gateway"]["auth_mode"] == "oauth"
        assert summary["devices"] == {"online": 0, "total": 0, "items": []}
        assert summary["setup"]["device_joined"] is False
        # one sign-in covers the PWA and the inbox API too
        assert client.get("/inbox/", follow_redirects=False).status_code == 200
        assert client.get("/api/inbox/threads").status_code == 200
        # cookie writes need X-ANX
        assert client.post("/api/invites", json={}).status_code == 401
        created = client.post("/api/invites", json={"label": "x"}, headers={"X-ANX": "1"})
        assert created.status_code == 201 and created.json()["code"].startswith("anx1.")
        assert client.get("/auth/login", follow_redirects=False).status_code == 303  # already signed in
        # an open redirect is never followed
        evil = login(client, next_path="//evil.test/")
        assert evil.headers["location"] == "/"
        assert client.post("/auth/logout").status_code == 403
        out = client.post("/auth/logout", headers={"X-ANX": "1"})
        assert out.status_code == 204 and "__Host-anx_inbox=" in out.headers["set-cookie"]
        client.cookies.clear()
        assert client.get("/api/console/summary").status_code == 401


def test_session_without_remember_and_rate_limit(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(oauth_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        session = login(client, remember=False)
        assert session.status_code == 303 and "max-age" not in session.headers["set-cookie"].lower()
        client.cookies.clear()
        for _ in range(8):
            assert login(client, "bad").status_code == 401
        limited = login(client)
        assert limited.status_code == 429 and "频繁" in limited.text


def test_bearer_only_gateway_signs_in_with_the_token(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(bearer_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        page = client.get("/auth/login")
        assert 'name="token"' in page.text and 'name="password"' not in page.text
        assert client.post("/auth/login", data={"token": "x" * 40}, headers={"Origin": ORIGIN}).status_code == 401
        ok = client.post("/auth/login", data={"token": SERVER_TOKEN, "remember": "1"}, headers={"Origin": ORIGIN},
                         follow_redirects=False)
        assert ok.status_code == 303
        assert client.get("/api/settings").json()["auth"]["mode"] == "bearer"
        # bearer header works on every owner API as well
        fresh = TestClient(client.app, base_url=ORIGIN)
        assert fresh.get("/api/devices", headers={"Authorization": f"Bearer {SERVER_TOKEN}"}).status_code == 200


def test_device_alias_and_revoke_of_a_legacy_device(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(bearer_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        with client.websocket_connect("/ingress/pc-main", headers={"Authorization": "Bearer " + "a" * 40}) as ws:
            ws.send_json({"protocol": 1, "agent_id": "pc-main", "catalog": {"workspaces": {}, "mcps": {},
                                                                              "device": {"hostname": "host-alpha", "os": "Windows 11"}},
                          "anx": {"version": "0.2.0", "features": []}})
            assert ws.receive_json()["method"] == "ready"
            auth = {"Authorization": f"Bearer {SERVER_TOKEN}"}
            devices = client.get("/api/devices", headers=auth).json()["devices"]
            assert devices[0]["device_id"] == "pc-main" and devices[0]["display_name"] == "host-alpha"
            assert devices[0]["enrolled_via"] == "agents_map" and devices[0]["version"] == "0.2.0" and devices[0]["online"]
            assert devices[0]["os"] == "Windows 11"
            renamed = client.put("/api/devices/pc-main", json={"alias": "工作站"}, headers=auth).json()["device"]
            assert renamed["display_name"] == "工作站" and renamed["enrolled_via"] == "agents_map"
            refused = client.post("/api/devices/pc-main/revoke", headers=auth)
            assert refused.status_code == 409 and refused.json()["code"] == "agents_map"
            assert client.post("/api/devices/nobody/revoke", headers=auth).status_code == 404
            summary = client.get("/api/console/summary", headers=auth).json()
            assert summary["devices"]["online"] == 1 and summary["setup"]["device_joined"]


def test_devices_list_the_plugins_each_device_publishes(tmp_path, monkeypatch):
    """The console's 插件 page: names, descriptions and tool counts per online device, nothing else."""
    tool = {"name": "search", "description": "检索", "inputSchema": {"type": "object", "properties": {}}}
    mcps = {"notes": {"description": "本机笔记", "tools": [tool, {**tool, "name": "open"}]},
            "broken": {"description": "", "tools": [], "error": "local MCP unavailable or authentication failed"}}
    with TestClient(build_cloud_app(bearer_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        auth = {"Authorization": f"Bearer {SERVER_TOKEN}"}
        with client.websocket_connect("/ingress/pc-main", headers={"Authorization": "Bearer " + "a" * 40}) as ws:
            ws.send_json({"protocol": 1, "agent_id": "pc-main", "catalog": {"workspaces": {}, "mcps": mcps,
                                                                              "device": {"hostname": "host-alpha"}},
                          "anx": {"version": "0.2.0", "features": []}})
            assert ws.receive_json()["method"] == "ready"
            device = client.get("/api/devices", headers=auth).json()["devices"][0]
            assert device["plugins"] == [{"name": "broken", "description": "", "tools": 0, "error": True},
                                         {"name": "notes", "description": "本机笔记", "tools": 2, "error": False}]
            assert device["plugins_count"] == 2
        offline = client.get("/api/devices", headers=auth).json()["devices"][0]
        assert offline["online"] is False and offline["plugins"] is None


def test_downloads_are_public_listed_hashed_and_streamed(tmp_path, monkeypatch):
    downloads = tmp_path / "state" / "downloads"
    downloads.mkdir(parents=True)
    setup = downloads / "agent-nexus_0.2.0_x64-setup.exe"
    setup.write_bytes(b"MZ" + b"x" * 1000)
    older = downloads / "agent-nexus_0.1.9_x64-setup.exe"
    older.write_bytes(b"MZold")
    deb = downloads / "agent-nexus_0.2.0_amd64.deb"
    deb.write_bytes(b"deb")
    gateway_zip = downloads / "agent-nexus-gateway-0.2.0.zip"
    gateway_zip.write_bytes(b"PK")
    (downloads / "notes.txt").write_text("not served")
    (downloads / "SHA256SUMS").write_text(f"{'a' * 64}  agent-nexus_0.2.0_amd64.deb\n")
    config = bearer_config(tmp_path, monkeypatch, downloads={"links": [{"label": "GitHub Releases", "url": "https://github.com/example/releases"}]})
    assert not [w for w in config_warnings(config) if "downloads" in w]
    with TestClient(build_cloud_app(config, tmp_path), base_url=ORIGIN) as client:
        listing = client.get("/api/downloads")  # no credentials
        assert listing.status_code == 200
        data = listing.json()
        assert data["latest"] == "0.2.0"
        names = [item["filename"] for item in data["items"]]
        assert "notes.txt" not in names and "SHA256SUMS" not in names and len(names) == 4
        by_name = {item["filename"]: item for item in data["items"]}
        main = by_name[setup.name]
        assert main["id"] == "windows-x64" and main["kind"] == "desktop" and main["latest"] and main["size"] == setup.stat().st_size
        assert main["sha256"] == hashlib.sha256(setup.read_bytes()).hexdigest() and main["url"] == f"/downloads/{setup.name}"
        assert by_name[older.name]["latest"] is False and by_name[older.name]["version"] == "0.1.9"
        assert by_name[deb.name]["sha256"] == "a" * 64  # from SHA256SUMS
        assert by_name[gateway_zip.name]["kind"] == "gateway"
        assert data["links"][0]["label"] == "GitHub Releases"
        streamed = client.get(f"/downloads/{setup.name}")
        assert streamed.status_code == 200 and streamed.content == setup.read_bytes()
        assert "attachment" in streamed.headers["content-disposition"]
        for bad in ("notes.txt", "..%2Fgateway.json", "missing.exe", "SHA256SUMS"):
            assert client.get(f"/downloads/{bad}").status_code == 404
        page = client.get("/downloads")
        assert page.status_code == 200 and "/console/downloads.js" in page.text
        assert client.get("/console/downloads.js").status_code == 200
        assert client.get("/console/../console.py").status_code == 404


def test_release_url_redirects_missing_files_and_empty_dir(tmp_path, monkeypatch):
    config = bearer_config(tmp_path, monkeypatch, downloads={"release_url": "https://github.com/example/repo/releases/download/v0.2.0"})
    with TestClient(build_cloud_app(config, tmp_path), base_url=ORIGIN) as client:
        data = client.get("/api/downloads").json()
        assert data["items"] == [] and data["links"][0]["url"].startswith("https://github.com/")
        moved = client.get("/downloads/agent-nexus_0.2.0_x64-setup.exe", follow_redirects=False)
        assert moved.status_code == 302 and moved.headers["location"].endswith("/v0.2.0/agent-nexus_0.2.0_x64-setup.exe")
    with pytest.raises(Exception, match="downloads.release_url"):
        build_cloud_app(bearer_config(tmp_path, monkeypatch, downloads={"release_url": "ftp://x"}), tmp_path)
    assert "unknown configuration key 'downloads.extra' (ignored)" in config_warnings({"downloads": {"extra": 1}})


def test_settings_lists_mcp_url_and_oauth_clients(tmp_path, monkeypatch):
    with TestClient(build_cloud_app(oauth_config(tmp_path, monkeypatch), tmp_path), base_url=ORIGIN) as client:
        registered = client.post("/register", json={"client_name": "ChatGPT", "redirect_uris": ["https://chatgpt.com/connector_platform_oauth_redirect"],
                                                     "token_endpoint_auth_method": "none", "grant_types": ["authorization_code"],
                                                     "response_types": ["code"]})
        login(client)
        settings = client.get("/api/settings").json()
        assert settings["gateway"]["mcp_url"] == f"{ORIGIN}/mcp" and settings["gateway"]["public_url"] == ORIGIN
        assert settings["invites"] == {"default_ttl_s": 86400, "max_ttl_s": 604800}
        if registered.status_code == 201:
            assert [c["kind"] for c in settings["clients"]] == ["gpt"]  # the session kind ChatGPT gets
        assert all(c["client_id"] != "anx-inbox" for c in settings["clients"])


def test_invite_settings_are_validated(tmp_path, monkeypatch):
    from mcpkit.config import ConfigError
    with pytest.raises(ConfigError, match="invites"):
        build_cloud_app(bearer_config(tmp_path, monkeypatch, invites={"default_ttl_s": 10}), tmp_path)
    with TestClient(build_cloud_app(bearer_config(tmp_path, monkeypatch, invites={"default_ttl_s": 3600, "max_ttl_s": 7200}),
                                    tmp_path), base_url=ORIGIN) as client:
        auth = {"Authorization": f"Bearer {SERVER_TOKEN}"}
        created = client.post("/api/invites", json={}, headers=auth).json()["invite"]
        assert round(created["expires_at"] - created["created_at"]) == 3600
        assert client.post("/api/invites", json={"ttl_s": 7300}, headers=auth).status_code == 400


def test_gateway_without_an_agents_map_accepts_invite_devices_only(tmp_path, monkeypatch):
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    config = {"token_env": "SERVER_TOKEN", "public_url": ORIGIN, "state_dir": str(tmp_path / "s")}
    with TestClient(build_cloud_app(config, tmp_path), base_url=ORIGIN) as client:
        auth = {"Authorization": f"Bearer {SERVER_TOKEN}"}
        code = client.post("/api/invites", json={}, headers=auth).json()["code"]
        from agent_nexus.invites import decode_invite
        invite = decode_invite(code)
        enrolled = client.post("/enroll", json={"invite_id": invite["id"], "secret": invite["secret"], "hostname": "host-beta"})
        assert enrolled.status_code == 201
        body = enrolled.json()
        assert body["agent_id"] == "host-beta" and body["hub"] == ORIGIN and len(body["token"]) >= 32
        with client.websocket_connect("/ingress/host-beta", headers={"Authorization": f"Bearer {body['token']}"}) as ws:
            ws.send_json({"protocol": 1, "agent_id": "host-beta", "catalog": {"workspaces": {}, "mcps": {}}})
            assert ws.receive_json()["method"] == "ready"
        # the whole code may be posted instead of its parts; a used one is 410
        again = client.post("/enroll", json={"invite": code, "hostname": "x"})
        assert again.status_code == 410 and again.json()["code"] == "used"
        assert client.post("/enroll", content=b"not json").status_code == 400


def test_console_assets_use_only_local_files():
    """No CDN at runtime: every page loads /ui/ and /console/ only (docs/ui-spec.md §0)."""
    pages = [STATIC / "console.html", STATIC / "downloads.html", STATIC / "index.html", STATIC / "pwa.html"]
    scripts = (list((STATIC / "console").glob("*.js")) + list((STATIC / "local").glob("*.js"))
               + [STATIC / "ui" / "inbox.js", STATIC / "ui" / "qr.js", STATIC / "ui" / "invite.js", STATIC / "ui" / "schema-form.js"])
    for path in pages + scripts:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"""(src|href)=["']https?://""", text), path.name
        for spec in re.findall(r"""from\s+["']([^"']+)["']""", text):
            assert spec.startswith(("/ui/", "/console/", "/local/", "./")), (path.name, spec)
    assert "Project Nayuki" in (Path(__file__).resolve().parents[1] / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
