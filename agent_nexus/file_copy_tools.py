"""The gateway's file copy tools (docs/design.md §18): ``file_copy`` starts a background job that
copies a file or a folder between two devices (or within one); ``file_copy_status`` /
``file_copy_cancel`` / ``file_copy_list`` follow it.

They reach nothing the caller could not reach before: the MCP caller authenticates as the owner,
who can already read and write every ``files/1`` device through the gateway's files relay, and a
device running a job acts with its own credential under the same lock rules as its 文件 page.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from mcp_types import ToolAnnotations

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from .gateway import Gateway

#: file_copy may overwrite (overwrite=true): destructive; it acts only on the owner's own devices.
COPY_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)
COPY_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
COPY_CANCEL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
FILE_COPY_TOOL_NAMES = ("file_copy", "file_copy_status", "file_copy_cancel", "file_copy_list")


def register(server: "FastMCP", gateway: "Gateway") -> None:
    service = gateway.file_copy

    @server.tool(title="Copy files between devices", annotations=COPY_WRITE)
    async def file_copy(src_device: str, src_path: str, dst_device: str, dst_path: str, overwrite: bool = False,
                        route: Literal["auto", "p2p"] = "auto", concurrency: int | None = None,
                        workspace_id: str | None = None) -> dict:
        """Copy, transfer or sync a file or a whole folder (recursively) from one device to another (or within
        one device); binary and multi-GB data, and folders of many thousands of files, are fine. Devices accept a
        display name or id. Paths are absolute in each device's own OS syntax (~ = that device's home). dst_path
        is what the source becomes; end it with a separator to copy into that folder. Runs as a background job
        and returns job_id at once: poll file_copy_status(job_id, wait_ms=30000) until state is completed,
        completed_with_errors, failed, cancelled or interrupted. If it ends interrupted or failed (e.g. a device
        went offline), or status says unknown, call file_copy again with the same arguments: finished files are
        skipped and partial ones resume. Repeating a copy is also how to sync later changes one way (new and
        changed files; nothing is ever deleted at the destination). Data goes directly between the devices when
        possible (P2P or a relay device), otherwise through the gateway (slow); route="p2p" fails instead of
        using the gateway. Every file is checked with sha256. overwrite=false never replaces a different
        existing file (it reports an error for it); overwrite=true replaces it, keeping its permissions.
        Symlinks inside the folder are not followed or copied. concurrency: files in parallel (1-8, default 4)."""
        await gateway._touch(workspace_id)
        return await service.start(src_device, src_path, dst_device, dst_path, overwrite=overwrite, route=route,
                                   concurrency=concurrency)

    @server.tool(title="Check a file copy", annotations=COPY_READ_ONLY)
    async def file_copy_status(job_id: str, wait_ms: int = 0, workspace_id: str | None = None) -> dict:
        """Progress and result of a file_copy job: state (scanning, copying, finishing; then completed,
        completed_with_errors, failed, cancelled or interrupted; unknown when the device running it cannot be
        reached right now), files {total, done, skipped, failed, in_progress}, bytes {total, done}, rate_bps,
        eta_s, current files, routes used, verified (sha256 / size), errors, message. wait_ms (up to 50000) waits
        until the job ends or wait_ms passes before answering. After interrupted, failed or unknown, calling
        file_copy again with the same arguments resumes the copy."""
        await gateway._touch(workspace_id)
        return await service.status(job_id, wait_ms)

    @server.tool(title="Cancel a file copy", annotations=COPY_CANCEL)
    async def file_copy_cancel(job_id: str, workspace_id: str | None = None) -> dict:
        """Cancel a running file_copy job. Files already copied stay; the file in progress is discarded."""
        await gateway._touch(workspace_id)
        return await service.cancel(job_id)

    @server.tool(title="List file copies", annotations=COPY_READ_ONLY)
    async def file_copy_list(workspace_id: str | None = None) -> dict:
        """List recent file_copy jobs (running and finished in the last 24 hours) on all online devices."""
        await gateway._touch(workspace_id)
        return await service.list()
