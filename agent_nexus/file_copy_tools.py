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
        """Copy a file or folder (recursively, any size) between devices or within one, as a background
        job; returns job_id: poll file_copy_status(job_id, wait_ms=30000). Paths are absolute on each device
        (~ = its home); a dst_path ending in a separator copies into that folder. Calling it again with the
        same arguments resumes or syncs (nothing is deleted). Details: docs/design.md §18."""
        await gateway._touch(workspace_id)
        return await service.start(src_device, src_path, dst_device, dst_path, overwrite=overwrite, route=route,
                                   concurrency=concurrency)

    @server.tool(title="Check a file copy", annotations=COPY_READ_ONLY)
    async def file_copy_status(job_id: str, wait_ms: int = 0, workspace_id: str | None = None) -> dict:
        """Progress of a file_copy job (state, files, bytes, errors); wait_ms (max 50000) waits for it to end.
        After interrupted, failed or unknown: run the same file_copy again to resume."""
        await gateway._touch(workspace_id)
        from .gateway import progress_heartbeat
        async with progress_heartbeat("file_copy_status"):
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
