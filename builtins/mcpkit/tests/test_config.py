import json

import pytest

from mcpkit import config
from mcpkit.config import ConfigError, load, read_secret, update_settings, write_secret


def test_central_entry_overrides_manifest_and_merges_auth_and_settings(workspace):
    directory = workspace.service("svc", manifest={
        "port": 9001, "path": "/mcp", "settings": {"a": 1, "b": 2},
        "auth": {"mode": "bearer", "bearer_secrets": ["svc/one"], "scopes": ["mcp", "x"]},
        "command": ["ignored"], "autostart": True,
    }, entry={"port": 9002, "settings": {"b": 3}, "auth": {"bearer_secrets": ["svc/two"]}})

    cfg = load(directory / "app.py")
    assert (cfg.name, cfg.port, cfg.root, cfg.dir) == ("svc", 9002, workspace.root.resolve(), directory.resolve())
    assert cfg.settings == {"a": 1, "b": 3}
    assert cfg.auth.mode == "bearer" and cfg.auth.bearer_secrets == ["svc/two"] and cfg.auth.scopes == ["mcp", "x"]
    assert load(directory, port=9100, host="localhost").port == 9100


def test_standalone_service_is_its_own_root(tmp_path):
    (tmp_path / "mcp-service.json").write_text(json.dumps({"name": "solo", "port": 1}), encoding="utf-8")
    cfg = load(tmp_path)
    assert cfg.root == tmp_path.resolve() and cfg.secrets_dir == tmp_path.resolve() / "secrets"


def test_mcpm_root_environment_wins(workspace, tmp_path_factory, monkeypatch):
    other = tmp_path_factory.mktemp("other")
    (other / "mcp-manager.json").write_text(json.dumps({"services": {"svc": {"port": 7777}}}), encoding="utf-8")
    directory = workspace.service("svc", manifest={"port": 1})
    monkeypatch.setenv("MCPM_ROOT", str(other))
    assert load(directory).port == 7777


def test_unknown_top_level_key_warns_but_does_not_block(workspace, capsys):
    directory = workspace.service("svc", manifest={"port": 1, "command": ["x"]}, entry={"publc_url": "https://x.example"})
    assert load(directory).public_url is None
    err = capsys.readouterr().err
    assert "unknown key(s) ignored" in err and "publc_url" in err and "command" not in err


@pytest.mark.parametrize("entry, message", [
    ({"auth": {"mode": "bearer"}}, "needs at least one entry in bearer_secrets"),
    ({"auth": {"mode": "oauth", "password_secret": "p"}}, "needs public_url"),
    ({"auth": {"mode": "oauth"}, "public_url": "https://x.example"}, "needs password_secret"),
    ({"public_url": "https://x.example/mcp"}, "https origin with no path"),
    ({"public_url": "http://x.example"}, "https origin with no path"),
    # Ported from payment-mcp: origins that look plausible but are not.
    ({"public_url": "https://user:password@x.example"}, "https origin with no path"),
    ({"public_url": "https://x.example?token=secret"}, "https origin with no path"),
    ({"public_url": "https://x.example/#fragment"}, "https origin with no path"),
    ({"public_url": "https://x.example:99999"}, "invalid port"),
    ({"auth": {"required_scopes": ["admin"]}}, "subset of auth.scopes"),
    ({"auth": {"mode": "bearer", "bearer_secrets": ["../escape"]}}, "invalid secret name"),
    ({"auth": {"typo": True}}, "typo"),
    ({"path": "mcp"}, "path must start with '/'"),
])
def test_invalid_configuration_is_explained(workspace, entry, message):
    directory = workspace.service("svc", manifest={"port": 1}, entry=entry)
    with pytest.raises(ConfigError, match=message):
        load(directory)


def test_loopback_public_url_may_use_http(workspace):
    directory = workspace.service("svc", manifest={"port": 1}, entry={"public_url": "http://127.0.0.1:5000/"})
    assert load(directory).public_url == "http://127.0.0.1:5000"


def test_secrets_are_validated_without_revealing_them(workspace):
    cfg = load(workspace.service("svc", manifest={"port": 1}))
    with pytest.raises(ConfigError, match="does not exist"):
        read_secret(cfg, "svc/missing")
    workspace.secret("svc/short", "abc")
    with pytest.raises(ConfigError, match="must be 32-4096") as short:
        read_secret(cfg, "svc/short")
    assert "abc" not in str(short.value)
    workspace.secret("svc/spaced", "a" * 20 + " " + "b" * 20)
    with pytest.raises(ConfigError, match="without whitespace"):
        read_secret(cfg, "svc/spaced")
    assert read_secret(cfg, "svc/short", min_length=3) == "abc"

    path = write_secret(cfg, "svc/generated")
    value = read_secret(cfg, "svc/generated")
    assert len(value) == 64 and path.is_file()
    assert write_secret(cfg, "svc/generated", "x" * 40) == path and read_secret(cfg, "svc/generated") == "x" * 40


def test_update_settings_preserves_everything_else(workspace):
    directory = workspace.service("svc", manifest={"port": 1},
                                  entry={"port": 5, "autostart": True, "settings": {"keep": "中文"}})
    workspace.central["manager"]["port"] = 8399
    cfg = load(directory)
    legacy = (workspace.root / "mcp-manager.json").read_bytes()
    update_settings(cfg, mode="summary")
    # Only the legacy file existed: it is left byte for byte, the result is agent-nexus.json.
    assert (workspace.root / "mcp-manager.json").read_bytes() == legacy
    data = json.loads((workspace.root / "agent-nexus.json").read_text(encoding="utf-8"))
    assert data["services"]["svc"] == {"port": 5, "autostart": True, "settings": {"keep": "中文", "mode": "summary"}}
    assert data["manager"] == {"port": 8399}
    assert load(directory).settings == {"keep": "中文", "mode": "summary"}


def test_oauth_store_defaults_under_secrets(workspace):
    workspace.secret("svc/pw", "p" * 40)
    directory = workspace.service("svc", manifest={"port": 1}, entry={
        "public_url": "https://svc.example", "auth": {"mode": "oauth", "password_secret": "svc/pw"}})
    cfg = load(directory)
    assert cfg.oauth_store == workspace.root.resolve() / "secrets" / "svc" / "oauth.sqlite3"
    assert cfg.resource == "https://svc.example/mcp"
    assert cfg.auth.redirect_uris == list(config.CHATGPT_REDIRECTS)


def test_settings_can_be_overridden_for_one_run(workspace, monkeypatch):
    directory = workspace.service("svc", manifest={"port": 1}, entry={"settings": {"profile": "official", "keep": 1}})
    monkeypatch.setenv("MCPKIT_SETTINGS", '{"profile": "full"}')
    assert load(directory).settings == {"profile": "full", "keep": 1}
    assert json.loads((workspace.root / "mcp-manager.json").read_text(encoding="utf-8")
                      )["services"]["svc"]["settings"] == {"profile": "official", "keep": 1}, "files must not change"
    monkeypatch.setenv("MCPKIT_SETTINGS", "not json")
    with pytest.raises(ConfigError, match="MCPKIT_SETTINGS is not valid JSON"):
        load(directory)


def test_windows_acl_output_does_not_use_utf8_reader_threads(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(config.sys,'platform','win32')
    monkeypatch.setattr(config.locale,'getencoding',lambda:'cp936')
    calls=[]
    def run(*args,**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0,stdout='已成功处理'.encode('cp936'),stderr=b'')
    monkeypatch.setattr(config.subprocess,'run',run)
    config.protect(tmp_path)
    assert calls[0].get('text',False) is False
    monkeypatch.setattr(config.subprocess,'run',lambda *a,**k:SimpleNamespace(
        returncode=1,stdout=b'',stderr='拒绝访问'.encode('cp936')))
    with pytest.raises(ConfigError,match='拒绝访问'):
        config.protect(tmp_path)
