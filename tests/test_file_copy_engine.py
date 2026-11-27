"""The copy engine (docs/design.md §18) between two local endpoints, plus faults injected around them."""
from __future__ import annotations

import asyncio
import hashlib
import os
import types

import pytest

from agent_nexus import file_copy
from agent_nexus.file_copy import CopyJob, LocalEndpoint, LocalSession, TransportError
from agent_nexus.file_share import CHUNK, FileShare, FileShareError, part_name


def share(tmp_path):
    account = types.SimpleNamespace(home=str(tmp_path), session=None, uid=os.getuid() if hasattr(os, "getuid") else 0,
                                    gid=os.getgid() if hasattr(os, "getgid") else 0)
    return FileShare(account=lambda: account)


def tree(root, big=3 * CHUNK + 7):
    (root / "a" / "b").mkdir(parents=True)
    (root / "empty" / "inner").mkdir(parents=True)
    (root / "a" / "big.bin").write_bytes(os.urandom(big))
    (root / "a" / "b" / "small.csv").write_bytes(b"t,o,h,l,c\n" * 50)
    for index in range(20):
        (root / "a" / f"m{index:02d}.csv").write_bytes(os.urandom(index * 300))
    return root


def digests(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir")
            for p in sorted(root.rglob("*")) if not p.name.endswith(".anxpart")}


def spec(src, dst, **extra):
    return {"src_device": "pc", "src_path": str(src), "dst_device": "pc2", "dst_path": str(dst), **extra}


def run(job):
    return asyncio.run(job.run())


def test_folder_copy_with_empty_folders_and_rerun_skips(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src")
    first = run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    assert first["state"] == "completed" and first["verified"] == "sha256", first
    assert digests(tmp_path / "dst") == digests(src)
    assert first["files"]["done"] == 22 and first["bytes"]["done"] == first["bytes"]["total"]
    again = run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    assert again["state"] == "completed" and again["files"]["skipped"] == 22 and again["files"]["done"] == 0


def test_destination_mapping_with_and_without_a_separator(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src", big=10)
    run(CopyJob(spec(src, str(tmp_path / "into") + os.sep), LocalEndpoint(files), LocalEndpoint(files)))
    assert digests(tmp_path / "into" / "src") == digests(src)
    single = run(CopyJob(spec(src / "a" / "big.bin", tmp_path / "one" / "renamed.bin"), LocalEndpoint(files),
                         LocalEndpoint(files)))
    assert single["state"] == "completed" and (tmp_path / "one" / "renamed.bin").read_bytes() == (src / "a" / "big.bin").read_bytes()
    run(CopyJob(spec(src / "a" / "big.bin", str(tmp_path / "one") + "/"), LocalEndpoint(files), LocalEndpoint(files)))
    assert (tmp_path / "one" / "big.bin").exists()


def test_overwrite_false_reports_and_overwrite_true_replaces(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src", big=10)
    (tmp_path / "dst" / "a").mkdir(parents=True)
    (tmp_path / "dst" / "a" / "big.bin").write_bytes(b"different")
    kept = run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    assert kept["state"] == "completed_with_errors" and kept["files"]["failed"] == 1
    assert "overwrite=false" in kept["errors"][0]["error"]
    assert (tmp_path / "dst" / "a" / "big.bin").read_bytes() == b"different"
    replaced = run(CopyJob(spec(src, tmp_path / "dst", overwrite=True), LocalEndpoint(files), LocalEndpoint(files)))
    assert replaced["state"] == "completed" and digests(tmp_path / "dst") == digests(src)


def test_a_partial_part_resumes_and_only_the_rest_is_read(tmp_path, monkeypatch):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    data = os.urandom(5 * CHUNK + 3)
    (src / "f.bin").write_bytes(data)
    info = (src / "f.bin").stat()
    the_spec = spec(src, tmp_path / "dst")
    key = file_copy.resume_key(the_spec, ["f.bin"], len(data), info.st_mtime_ns, ["f.bin"])
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / part_name("f.bin", key)).write_bytes(data[:2 * CHUNK])
    offsets = []
    original = LocalSession.read

    async def read(self, base, rel, offset):
        offsets.append(offset)
        return await original(self, base, rel, offset)
    monkeypatch.setattr(LocalSession, "read", read)
    result = run(CopyJob(the_spec, LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "completed" and offsets == [2 * CHUNK]
    assert (tmp_path / "dst" / "f.bin").read_bytes() == data and result["verified"] == "sha256"


class Flaky(LocalEndpoint):
    """A destination whose route breaks once in the middle of a file."""

    def __init__(self, share_):
        super().__init__(share_)
        self.broken = 0
        self.resets = 0

    def session(self):
        endpoint = self

        class Session(LocalSession):
            async def put(self, base, rel, *, open_source, **kw):
                if endpoint.broken == 0 and rel[-1] == "big.bin":
                    endpoint.broken += 1

                    async def opened(offset):
                        reader = await open_source(offset)
                        inner = reader.chunks

                        async def chunks():
                            count = 0
                            async for data in inner:
                                count += 1
                                if count == 2:
                                    raise TransportError("tunnel dropped")
                                yield data
                        reader.chunks = chunks()
                        return reader
                    return await super().put(base, rel, open_source=opened, **kw)
                return await super().put(base, rel, open_source=open_source, **kw)

            async def reset(self):
                endpoint.resets += 1
        return Session(self)


def test_a_transport_failure_repicks_the_route_and_resumes(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src")
    dst = Flaky(files)
    result = run(CopyJob(spec(src, tmp_path / "dst", backoff_scale=0.0), LocalEndpoint(files), dst))
    assert result["state"] == "completed", result
    assert dst.broken == 1 and dst.resets == 1
    assert digests(tmp_path / "dst") == digests(src)


def test_disk_full_fails_the_job_and_leaves_no_part(tmp_path, monkeypatch):
    files = share(tmp_path)
    src = tree(tmp_path / "src")
    original = files.write_bytes

    def write_bytes(params, data):
        upload = files.uploads[params["id"]]
        if upload.target.name == "big.bin" and upload.written >= CHUNK:
            files._discard(params["id"], discard=True)
            raise FileShareError("目标磁盘已满", "nospace")
        return original(params, data)
    monkeypatch.setattr(files, "write_bytes", write_bytes)
    result = run(CopyJob(spec(src, tmp_path / "dst", concurrency=1), LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "failed" and "磁盘已满" in result["message"]
    assert not list((tmp_path / "dst").rglob("*.anxpart"))


def test_free_space_is_checked_before_any_byte_moves(tmp_path, monkeypatch):
    files = share(tmp_path)
    src = tree(tmp_path / "src")
    original = files.stat_many
    monkeypatch.setattr(files, "stat_many", lambda params: {**original(params), "free": 100})
    result = run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "failed" and "空间不足" in result["message"]
    assert not (tmp_path / "dst").exists()


def test_cancel_midway_leaves_no_part(tmp_path, monkeypatch):
    files = share(tmp_path)
    src = tree(tmp_path / "src", big=8 * CHUNK)
    original = files.write_bytes

    def slow(params, data):
        import time
        time.sleep(0.05)
        return original(params, data)
    monkeypatch.setattr(files, "write_bytes", slow)

    async def main():
        job = CopyJob(spec(src, tmp_path / "dst", concurrency=2), LocalEndpoint(files), LocalEndpoint(files))
        job.start()
        while job.bytes["done"] < CHUNK:
            await asyncio.sleep(0.02)
        await job.cancel()
        return job.snapshot()

    result = asyncio.run(main())
    assert result["state"] == "cancelled"
    assert not list((tmp_path / "dst").rglob("*.anxpart"))


def test_a_source_modified_during_the_copy_is_detected(tmp_path, monkeypatch):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "live.log").write_bytes(b"x" * 1000)
    original = LocalSession.read

    async def read(self, base, rel, offset):
        reader = await original(self, base, rel, offset)
        with open(src / "live.log", "ab") as handle:  # someone keeps appending
            handle.write(b"more")
        return reader
    monkeypatch.setattr(LocalSession, "read", read)
    result = run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "completed_with_errors"
    assert "被修改" in result["errors"][0]["error"]
    assert not list((tmp_path / "dst").rglob("*.anxpart"))


def test_a_copy_into_itself_is_refused(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src", big=10)
    result = run(CopyJob(spec(src, src / "a" / "copy", same_device=True), LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "failed" and "自身" in result["message"]


class Counting(LocalEndpoint):
    def __init__(self, share_):
        super().__init__(share_)
        self.active = self.peak = 0
        self.largest = 0

    def session(self):
        endpoint = self

        class Session(LocalSession):
            async def put(self, base, rel, *, open_source, **kw):
                endpoint.active += 1
                endpoint.peak = max(endpoint.peak, endpoint.active)
                try:
                    async def opened(offset):
                        reader = await open_source(offset)
                        inner = reader.chunks

                        async def chunks():
                            async for data in inner:
                                endpoint.largest = max(endpoint.largest, len(data))
                                await asyncio.sleep(0.001)
                                yield data
                        reader.chunks = chunks()
                        return reader
                    return await super().put(base, rel, open_source=opened, **kw)
                finally:
                    endpoint.active -= 1
        return Session(self)


def test_concurrency_and_chunk_size_are_bounded(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src")
    dst = Counting(files)
    result = run(CopyJob(spec(src, tmp_path / "dst", concurrency=3), LocalEndpoint(files), dst))
    assert result["state"] == "completed"
    assert 1 < dst.peak <= 3 and dst.largest <= CHUNK


def test_status_long_poll_wakes_on_completion(tmp_path):
    files = share(tmp_path)
    src = tree(tmp_path / "src", big=10)

    async def main():
        job = CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files))
        job.start()
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not job.done and loop.time() - started < 10:
            await job.wait(30)
        return job.done, loop.time() - started

    done, took = asyncio.run(main())
    assert done and took < 10


def test_classify_maps_errors():
    from agent_nexus.file_transfer import TransferError
    from agent_nexus.federation import RoutingError
    assert isinstance(file_copy.classify(TransferError("x")), TransportError)
    assert file_copy.classify(TransferError("x", 422, "exists")).code == "exists"
    assert file_copy.classify(RoutingError("locked: 这台设备已锁定")).code == "locked"
    assert isinstance(file_copy.classify(RoutingError("manager x is offline")), TransportError)
    assert file_copy.classify(OSError(28, "No space left on device")).code == "nospace"
    with pytest.raises(AssertionError):
        assert isinstance(file_copy.classify(PermissionError("p")), TransportError)


def test_files_config_switches(tmp_path):
    import json
    from agent_nexus.registry import load_manager_config
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"files": {"jobs": False}}), encoding="utf-8")
    cfg = load_manager_config(tmp_path)
    assert cfg.files is True and cfg.files_jobs is False
    (tmp_path / "agent-nexus.json").write_text(json.dumps({"files": {"enabled": False}}), encoding="utf-8")
    cfg = load_manager_config(tmp_path)
    assert cfg.files is False and cfg.files_jobs is False
    (tmp_path / "agent-nexus.json").write_text("{}", encoding="utf-8")
    assert load_manager_config(tmp_path).files_jobs is True


def test_device_jobs_survive_a_restart_as_interrupted(tmp_path):
    import json
    from agent_nexus.file_copy_device import CopyJobs, Links
    state = tmp_path / ".anx" / "fxjobs"
    state.mkdir(parents=True)
    (state / "pc-a_x.json").write_text(json.dumps({"job_id": "pc-a:x", "state": "copying", "created_at": 1e12}),
                                       encoding="utf-8")
    jobs = CopyJobs(tmp_path, share(tmp_path), links=Links(None, lambda d: None))
    snap = asyncio.run(jobs.status({"job_id": "pc-a:x"}))
    assert snap["state"] == "interrupted" and "续传" in snap["message"]
    assert asyncio.run(jobs.list())["jobs"][0]["job_id"] == "pc-a:x"


# -- review fixes --------------------------------------------------------------------------------

def test_a_retry_after_a_broken_route_goes_on_from_the_part(tmp_path, monkeypatch):
    """The second attempt starts where the first left the part, not from byte 0."""
    files = share(tmp_path)
    src = tree(tmp_path / "src", big=6 * CHUNK + 5)
    offsets = []
    original = LocalSession.read

    async def read(self, base, rel, offset):
        if rel[-1] == "big.bin":
            offsets.append(offset)
        return await original(self, base, rel, offset)
    monkeypatch.setattr(LocalSession, "read", read)
    dst = Flaky(files)
    result = run(CopyJob(spec(src, tmp_path / "dst", backoff_scale=0.0), LocalEndpoint(files), dst))
    assert result["state"] == "completed" and result["verified"] == "sha256", result
    assert len(offsets) == 2 and offsets[0] == 0 and offsets[1] >= CHUNK, offsets
    assert digests(tmp_path / "dst") == digests(src)


class ShareTransport:
    """``fx_*`` straight on a FileShare, as the gateway pump sees a device (errors as TransferError)."""

    def __init__(self, files, broken=()):
        self.files = files
        self.broken = set(broken)
        self.calls = []

    async def rpc(self, method, params, timeout=None):
        from agent_nexus.file_transfer import TransferError
        self.calls.append(method)
        if method in self.broken:
            raise TransferError("timed out")
        handler = self.files.handlers()[method]
        result = await handler(params)
        if "error" in result:
            raise TransferError(result["error"], 422, result.get("code") or "",
                                {k: v for k, v in result.items() if k not in ("error", "code")})
        return result

    async def read(self, params):
        import base64
        result = await self.rpc("fx_read", params)
        return base64.b64decode(result["data"]), result

    async def write(self, upload_id, offset, data):
        return await asyncio.to_thread(self.files.write_bytes, {"id": upload_id, "offset": offset}, data)


def test_a_resumed_relay_read_without_the_source_digest_is_sent_again_not_kept(tmp_path):
    """fx_hash fails (a timeout on a big file): the stale part must not be renamed into place."""
    from agent_nexus.file_copy import RelayEndpoint
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    data = os.urandom(3 * CHUNK + 5)
    (src / "f.bin").write_bytes(data)
    info = (src / "f.bin").stat()
    the_spec = spec(src, tmp_path / "dst")
    key = file_copy.resume_key(the_spec, ["f.bin"], len(data), info.st_mtime_ns, ["f.bin"])
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / part_name("f.bin", key)).write_bytes(b"\0" * CHUNK)  # stale, not the source's bytes
    transport = ShareTransport(files, broken={"fx_hash"})
    result = run(CopyJob(the_spec, RelayEndpoint(transport, v2=True), LocalEndpoint(files)))
    assert result["state"] == "completed", result
    assert (tmp_path / "dst" / "f.bin").read_bytes() == data and result["verified"] == "sha256"
    assert "fx_hash" in transport.calls


class Dead(LocalEndpoint):
    """A destination whose route is always broken (the far device went to sleep)."""

    def __init__(self, share_):
        super().__init__(share_)
        self.puts = 0

    def session(self):
        endpoint = self

        class Session(LocalSession):
            async def put(self, base, rel, **kw):
                endpoint.puts += 1
                raise TransportError("tunnel dropped")
        return Session(self)


def test_a_far_device_that_stays_offline_stops_the_job(tmp_path):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    for index in range(40):
        (src / f"{index:02d}.csv").write_bytes(b"x" * (index + 1))
    dst = Dead(files)
    result = run(CopyJob(spec(src, tmp_path / "dst", backoff_scale=0.0), LocalEndpoint(files), dst))
    assert result["state"] == "failed" and "离线" in result["message"], result
    assert dst.puts <= (file_copy.OFFLINE_AFTER + 1 + 4) * file_copy.ATTEMPTS, dst.puts
    assert result["files"]["failed"] < 40


def test_mtimes_coarsened_by_the_destination_still_count_as_the_same_file(tmp_path):
    from agent_nexus.file_share import same_mtime
    exact = 1_700_000_000_123_456_789
    assert same_mtime(exact, exact)
    assert same_mtime(exact, exact // 100 * 100)  # NTFS
    assert same_mtime(exact, exact // 10**9 * 10**9)  # SMB / HFS+
    assert same_mtime(exact, (exact // (2 * 10**9) + 1) * 2 * 10**9)  # FAT rounds up to 2 s
    assert not same_mtime(exact, exact + 5_000_000_000)
    assert not same_mtime(exact, exact + 1)
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.csv").write_bytes(b"abc")
    (src / "b.csv").write_bytes(b"defg")
    os.utime(src / "a.csv", ns=(exact, exact))
    os.utime(src / "b.csv", ns=(exact, exact))
    run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    coarse = exact // 10**9 * 10**9
    os.utime(tmp_path / "dst" / "a.csv", ns=(coarse, coarse))
    os.utime(tmp_path / "dst" / "b.csv", ns=(exact // 100 * 100,) * 2)
    again = run(CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files)))
    assert again["state"] == "completed" and again["files"]["skipped"] == 2, again


def test_the_status_long_poll_waits_for_the_end_not_the_next_file(tmp_path, monkeypatch):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    for index in range(30):
        (src / f"{index:02d}.csv").write_bytes(b"x" * 10)
    original = files.write_bytes

    def slow(params, data):
        import time
        time.sleep(0.05)
        return original(params, data)
    monkeypatch.setattr(files, "write_bytes", slow)

    async def main():
        job = CopyJob(spec(src, tmp_path / "dst", concurrency=1), LocalEndpoint(files), LocalEndpoint(files))
        job.start()
        loop = asyncio.get_running_loop()
        began = loop.time()
        await job.wait_done(0.6)
        took = loop.time() - began
        done_early = job.done
        await job.wait_done(30)
        return took, done_early, job.done

    took, done_early, done = asyncio.run(main())
    assert not done_early and took >= 0.55 and done


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_a_symlink_planted_at_the_part_name_is_not_followed(tmp_path):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "big").write_bytes(b"A" * 100)
    info = (src / "big").stat()
    the_spec = spec(src, tmp_path / "dst")
    key = file_copy.resume_key(the_spec, ["big"], 100, info.st_mtime_ns, ["big"])
    (tmp_path / "dst").mkdir()
    victim = tmp_path / "victim"
    victim.write_bytes(b"VICTIM-CONTENT\n")
    os.symlink(victim, tmp_path / "dst" / part_name("big", key))
    result = run(CopyJob(the_spec, LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "completed", result
    assert victim.read_bytes() == b"VICTIM-CONTENT\n"
    assert (tmp_path / "dst" / "big").read_bytes() == b"A" * 100 and not (tmp_path / "dst" / "big").is_symlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_modes_are_carried_and_an_overwrite_keeps_the_replaced_files_mode(tmp_path):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "secret.key").write_bytes(b"k" * 10)
    os.chmod(src / "secret.key", 0o600)
    (src / "run.sh").write_bytes(b"#!/bin/sh\n")
    os.chmod(src / "run.sh", 0o755)
    (src / "shadow").write_bytes(b"new")
    (tmp_path / "dst").mkdir()
    (tmp_path / "dst" / "shadow").write_bytes(b"old content")
    os.chmod(tmp_path / "dst" / "shadow", 0o640)
    result = run(CopyJob(spec(src, tmp_path / "dst", overwrite=True), LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "completed", result
    mode = lambda name: (tmp_path / "dst" / name).stat().st_mode & 0o777
    assert mode("secret.key") == 0o600 and mode("run.sh") == 0o755 & ~file_copy_umask()
    assert mode("shadow") == 0o640 and (tmp_path / "dst" / "shadow").read_bytes() == b"new"


def file_copy_umask():
    from agent_nexus.file_share import UMASK
    return UMASK


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_a_source_file_swapped_for_a_symlink_during_the_copy_is_not_read(tmp_path):
    files = share(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"public")
    secret = tmp_path / "rootonly"
    secret.write_bytes(b"TOP-SECRET-ROOT-DATA")

    async def main():
        job = CopyJob(spec(src, tmp_path / "dst"), LocalEndpoint(files), LocalEndpoint(files))
        plan = await job._plan()
        await job._decide(plan)
        (src / "a.txt").unlink()
        os.symlink(secret, src / "a.txt")
        await job._copy(plan)
        return job.snapshot()

    result = asyncio.run(main())
    assert result["errors"], result
    copied = tmp_path / "dst" / "a.txt"
    assert not copied.exists() or b"SECRET" not in copied.read_bytes()


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="POSIX symlinks")
def test_a_destination_folder_swapped_for_a_symlink_is_not_written_through(tmp_path):
    files = share(tmp_path)
    (tmp_path / "dst").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, tmp_path / "dst" / "sub")
    with pytest.raises(FileShareError) as refused:
        files.begin({"base": str(tmp_path / "dst"), "rel": ["sub", "f.txt"], "size": 1, "on_exists": "error"})
    assert refused.value.code == "link" and not list(elsewhere.iterdir())
    stats = files.stat_many({"base": str(tmp_path / "dst"), "rels": [["sub", "f.txt"], ["sub"]], "keys": []})
    assert stats["items"][0].get("code") == "link" and stats["items"][1]["link"] is True


def test_a_same_device_copy_into_itself_is_refused_with_a_tilde_destination(tmp_path):
    files = share(tmp_path)  # home = tmp_path
    src = tree(tmp_path / "data", big=10)
    result = run(CopyJob(spec(src, "~/data/inner", same_device=True), LocalEndpoint(files), LocalEndpoint(files)))
    assert result["state"] == "failed" and "自身" in result["message"], result
    assert not (src / "inner").exists()


def test_an_old_destination_with_a_huge_folder_reports_instead_of_duplicating(tmp_path):
    from agent_nexus.file_copy import RelayEndpoint

    class Listing:
        async def rpc(self, method, params, timeout=None):
            assert method == "fx_list"
            return {"path": params["path"], "sep": "/", "truncated": True,
                    "entries": [{"name": f"{i}.csv", "dir": False, "size": 1, "mtime": 0} for i in range(5000)]}

    endpoint = RelayEndpoint(Listing(), v2=False)
    answer = asyncio.run(endpoint.stat_many("/data", [["7.csv"], ["9999.csv"]], [None, None]))
    assert answer["items"][0]["exists"] is True
    assert answer["items"][1].get("error") and "旧版" in answer["items"][1]["error"]


def test_a_resumed_local_read_hashes_the_prefix_before_it_returns(tmp_path):
    data = os.urandom(2 * CHUNK + 9)
    (tmp_path / "f").write_bytes(data)

    async def main():
        reader = await file_copy.local_reader(tmp_path / "f", CHUNK + 3)
        body = b"".join([part async for part in reader.chunks])
        return body, reader.digest()

    body, digest = asyncio.run(main())
    assert body == data[CHUNK + 3:] and digest == hashlib.sha256(data).hexdigest()


def test_an_idle_put_stream_gets_keepalives_while_the_source_prepares(monkeypatch):
    from agent_nexus import file_copy_device
    monkeypatch.setattr(file_copy_device, "KEEPALIVE_S", 0.05)

    class Stream:
        sent = 0

        async def keepalive(self):
            Stream.sent += 1

    async def slow():
        await asyncio.sleep(0.3)
        return "reader"

    assert asyncio.run(file_copy_device.keep_alive(Stream(), slow())) == "reader"
    assert Stream.sent >= 3
