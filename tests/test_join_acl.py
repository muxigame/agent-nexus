"""Joining keeps the device token under an owner-only ACL without trusting USERDOMAIN/USERNAME,
and never spends an invite when this root cannot keep the token."""
from __future__ import annotations

import json
import subprocess
import sys

import httpx
import pytest
from mcpkit import config
from mcpkit.config import ConfigError

from agent_nexus import join as joining
from agent_nexus.invites import encode_invite

INVITE = encode_invite("https://hub.example.test", "inv_abcdefgh", "x" * 43)


class FakeHub:
    posts: list = []

    def __init__(self, **_):
        pass

    def post(self, url, json=None):
        FakeHub.posts.append(url)
        return httpx.Response(201, json={"agent_id": "acl-box", "token": "t" * 43, "display_name": "acl-box"})

    def close(self):
        pass


@pytest.fixture
def hub(monkeypatch):
    FakeHub.posts = []
    monkeypatch.setattr(httpx, "Client", FakeHub)
    return FakeHub


def root_for(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({"cloud": {"enabled": False}}), encoding="utf-8")
    return root


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_token_is_protected_by_sid_even_with_misleading_account_variables(tmp_path, monkeypatch, hub):
    # A launcher or service environment whose variables do not name the real account.
    monkeypatch.setenv("USERDOMAIN", "WORKGROUP")
    monkeypatch.setenv("USERNAME", "someone-else")
    assert config.current_user_principal().startswith("*S-1-")
    root = root_for(tmp_path)
    result = joining.join(root, INVITE, hostname="acl-box")
    assert result["agent_id"] == "acl-box"
    token = root / "secrets" / "cloud" / "device-token"
    assert token.read_text(encoding="utf-8").strip() == "t" * 43
    acl = subprocess.run(["icacls", str(token)], capture_output=True).stdout.decode("mbcs", "replace")
    assert "NT AUTHORITY\\SYSTEM" in acl or "S-1-5-18" in acl
    assert "Everyone" not in acl and "BUILTIN\\Users" not in acl


def test_principal_falls_back_to_the_account_name(monkeypatch):
    monkeypatch.setattr(config, "_token_user_sid", lambda: None)
    monkeypatch.setattr(config, "_whoami_sid", lambda: None)
    monkeypatch.setenv("USERDOMAIN", "BOX")
    monkeypatch.setenv("USERNAME", "alice")
    assert config.current_user_principal() == "BOX\\alice"
    monkeypatch.setattr(config, "_whoami_sid", lambda: "S-1-5-21-1-2-3-1001")
    assert config.current_user_principal() == "*S-1-5-21-1-2-3-1001"


def test_a_root_that_cannot_keep_the_token_does_not_spend_the_invite(tmp_path, monkeypatch, hub):
    def refuse(path):
        raise ConfigError(f"could not restrict access to {path}: No mapping between account names and security IDs")

    monkeypatch.setattr(joining, "protect", refuse)
    root = root_for(tmp_path)
    with pytest.raises(joining.JoinError) as failed:
        joining.join(root, INVITE, hostname="acl-box")
    assert failed.value.code == "write_failed"
    assert "邀请码未使用" in failed.value.message and "No mapping" in failed.value.message
    assert hub.posts == []  # /enroll was never called
    assert json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"] == {"enabled": False}


def test_a_failure_after_enroll_names_the_cause(tmp_path, monkeypatch, hub):
    def broken(*_args, **_kwargs):
        raise PermissionError(13, "Access is denied", "secrets/cloud/device-token")

    monkeypatch.setattr(joining, "write_secret", broken)
    with pytest.raises(joining.JoinError) as failed:
        joining.join(root_for(tmp_path), INVITE, hostname="acl-box")
    assert "Access is denied" in failed.value.message and "device-token" in failed.value.message
