from types import SimpleNamespace
import sys
import pytest
from mcpkit.runtime import install_windows_socket_cleanup


def reset(code=10054):
    error=ConnectionResetError(code,'reset')
    error.winerror=code
    return error


def test_windows_reset_completes_close_and_server_detach():
    if sys.platform!='win32':
        pytest.skip('requires the Windows proactor transport')
    from asyncio.proactor_events import _ProactorBasePipeTransport
    events=[]
    class Socket:
        def shutdown(self,how): raise reset()
        def fileno(self): return 10
        def close(self): events.append('closed')
    transport=SimpleNamespace(_sock=Socket(),_called_connection_lost=False,
        _protocol=SimpleNamespace(connection_lost=lambda exc:events.append('protocol')),
        _server=SimpleNamespace(_detach=lambda:events.append('detached')))
    install_windows_socket_cleanup()
    method=_ProactorBasePipeTransport._call_connection_lost
    install_windows_socket_cleanup()
    assert _ProactorBasePipeTransport._call_connection_lost is method
    method(transport,None)
    assert events==['protocol','closed','detached']
    assert transport._sock is None and transport._server is None and transport._called_connection_lost
    method(transport,None)
    assert len(events)==3


def test_unexpected_socket_errors_are_preserved():
    if sys.platform!='win32': pytest.skip('requires Windows')
    from asyncio.proactor_events import _ProactorBasePipeTransport
    class Socket:
        def shutdown(self,how): raise reset(1234)
        def fileno(self): return 10
        def close(self): pass
    transport=SimpleNamespace(_sock=Socket(),_called_connection_lost=False,
        _protocol=SimpleNamespace(connection_lost=lambda exc:None),_server=None)
    install_windows_socket_cleanup()
    with pytest.raises(ConnectionResetError):
        _ProactorBasePipeTransport._call_connection_lost(transport,None)


def test_protocol_errors_are_not_hidden():
    if sys.platform!='win32':
        pytest.skip('requires Windows')
    from asyncio.proactor_events import _ProactorBasePipeTransport
    closed=[]
    def protocol_error(exc): raise RuntimeError('real application error')
    socket=SimpleNamespace(shutdown=lambda how:None,fileno=lambda:10,close=lambda:closed.append(1))
    transport=SimpleNamespace(_sock=socket,_called_connection_lost=False,
        _protocol=SimpleNamespace(connection_lost=protocol_error),_server=None)
    install_windows_socket_cleanup()
    with pytest.raises(RuntimeError,match='real application error'):
        _ProactorBasePipeTransport._call_connection_lost(transport,None)
    assert closed==[1]
