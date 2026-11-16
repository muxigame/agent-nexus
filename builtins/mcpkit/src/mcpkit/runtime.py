"""Narrow compatibility fix for Windows Proactor socket teardown."""
from __future__ import annotations

import socket
import sys


def install_windows_socket_cleanup() -> None:
    if sys.platform != "win32":
        return
    from asyncio.proactor_events import _ProactorBasePipeTransport
    if getattr(_ProactorBasePipeTransport._call_connection_lost, "_mcpkit_reset_cleanup", False):
        return

    def cleanup(transport, exc):
        if transport._called_connection_lost:
            return
        try:
            transport._protocol.connection_lost(exc)
        finally:
            try:
                if hasattr(transport._sock, "shutdown") and transport._sock.fileno() != -1:
                    transport._sock.shutdown(socket.SHUT_RDWR)
            except ConnectionResetError as reset:
                if getattr(reset, "winerror", None) != 10054:
                    raise
            finally:
                # This is Python's original cleanup sequence. It must run even
                # after an already-reset peer rejects shutdown().
                transport._sock.close()
                transport._sock = None
                server = transport._server
                if server is not None:
                    server._detach()
                    transport._server = None
                transport._called_connection_lost = True

    cleanup._mcpkit_reset_cleanup = True
    _ProactorBasePipeTransport._call_connection_lost = cleanup
