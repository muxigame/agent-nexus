"""file_copy against the files/1 share that 0.8.30 devices actually run (a frozen copy of
agent_nexus/file_share.py at 0.8.30, tests/compat/fixtures/legacy_file_share_0_8_30.py)."""
from __future__ import annotations

import hashlib
import importlib.util
import os
import types
from pathlib import Path

from test_file_copy_gateway import FakePeer, digests, finish, run, tree

FROZEN = Path(__file__).resolve().parent / "compat" / "fixtures" / "legacy_file_share_0_8_30.py"
FROZEN_SHA256 = "312da23ab8a1d3dcca436e7732b69ed424ae908884417934e7f2b21607abed7c"


def legacy_module():
    spec = importlib.util.spec_from_file_location("legacy_file_share_0_8_30", FROZEN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OldPeer(FakePeer):
    """A 0.8.30 device: files/1 only, served by the frozen share."""

    def __init__(self, hub, agent_id, root):
        super().__init__(hub, agent_id, root, features=("files/1",))
        account = types.SimpleNamespace(home=str(self.home), session=None, uid=os.getuid(), gid=os.getgid())
        self.share = legacy_module().FileShare(account=lambda: account)
        self.handlers = self.share.handlers()


def test_the_frozen_share_is_untouched():
    assert hashlib.sha256(FROZEN.read_bytes()).hexdigest() == FROZEN_SHA256
    assert set(OldPeerHandlers()) == {"fx_list", "fx_read", "fx_put_begin", "fx_put_write", "fx_put_end", "fx_put_abort"}


def OldPeerHandlers():
    return legacy_module().FileShare().handlers()


def test_push_to_pull_from_and_pump_between_old_devices_and_reruns_skip(tmp_path):
    async def body(hub, gateway, client):
        new = FakePeer(hub, "pc-new", tmp_path)
        old = OldPeer(hub, "pc-old", tmp_path)
        old2 = OldPeer(hub, "pc-old2", tmp_path)
        src_new = tree(new.home)
        src_old = tree(old.home)
        results = {}
        for name, args in {
            "push": ("pc-new", str(src_new), "pc-old", str(old.home / "pushed")),
            "pull": ("pc-old", str(src_old), "pc-new", str(new.home / "pulled")),
            "pump": ("pc-old", str(src_old), "pc-old2", str(old2.home / "pumped")),
        }.items():
            arguments = dict(zip(("src_device", "src_path", "dst_device", "dst_path"), args))
            started = (await client.call_tool("file_copy", arguments)).data
            first = await finish(client, started["job_id"])
            again = (await client.call_tool("file_copy", arguments)).data
            second = await finish(client, again["job_id"])
            results[name] = (started, first, second)
        return new, old, old2, src_new, src_old, results

    new, old, old2, src_new, src_old, results = run(body)
    assert results["push"][0]["mode"] == "push" and results["pull"][0]["mode"] == "pull"
    assert results["pump"][0]["mode"] == "gateway"
    for name, (_started, first, second) in results.items():
        assert first["state"] == "completed", (name, first)
        assert second["state"] == "completed" and second["files"]["done"] == 0, (name, second)
        assert second["files"]["skipped"] == first["files"]["done"] == 13, (name, second)
    assert results["push"][1]["verified"] == "size" and results["pull"][1]["verified"] == "sha256"
    # an old destination cannot create empty folders (a warning says so); everything else is there
    without_empty = {k: v for k, v in digests(src_new).items() if k != "empty"}
    assert digests(old.home / "pushed") == without_empty
    assert digests(new.home / "pulled") == digests(src_old)
    assert digests(old2.home / "pumped") == {k: v for k, v in digests(src_old).items() if k != "empty"}
    assert any("空文件夹" in w for w in results["push"][1]["warnings"])
