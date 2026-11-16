import socket
import subprocess
import sys
import time
import json
from pathlib import Path
import psutil
import pytest
from agent_nexus.supervisor import Supervisor,SupervisorError,listener_of


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def wait(predicate):
    end=time.monotonic()+15
    while time.monotonic()<end:
        if predicate(): return True
        time.sleep(.1)
    return False


def service(root,name,number):
    directory=root/name
    directory.mkdir()
    (directory/'mcp-service.json').write_text(json.dumps({'command':[sys.executable,'-m','http.server','{port}','--bind','127.0.0.1'],
        'port':number,'autorestart':True}),encoding='utf-8')
    return directory


def test_shutdown_stops_owned_and_adopted_services_without_restart(tmp_path):
    numbers=[port(),port()]
    for name,number in zip(('a','b'),numbers): service(tmp_path,name,number)
    first=Supervisor(tmp_path)
    for name in ('a','b'): first.start(name)
    assert wait(lambda:all(listener_of('127.0.0.1',p)[0] for p in numbers))
    adopted=Supervisor(tmp_path)
    adopted.run_background()
    report=adopted.shutdown()
    assert wait(lambda:all(not listener_of('127.0.0.1',p)[0] for p in numbers))
    assert all(adopted.rt[name].pid is None and not adopted.rt[name].want_running for name in ('a','b'))
    assert not list((tmp_path/'.anx/run').glob('*.json'))
    with pytest.raises(SupervisorError,match='shutting down'): adopted.start('a')
    assert all(line.startswith(name+' stopped') for name,line in zip(('a','b'),report))
    assert adopted.shutdown()==report


def test_shutdown_preserves_unmanaged_external_server(tmp_path):
    number=port()
    directory=service(tmp_path,'external',number)
    outsider=subprocess.Popen([sys.executable,'-m','http.server',str(number),'--bind','127.0.0.1'],
        cwd=directory,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        assert wait(lambda:listener_of('127.0.0.1',number)[0])
        supervisor=Supervisor(tmp_path)
        supervisor.tick()
        assert supervisor.snapshot('external')['status']=='external'
        supervisor.shutdown()
        assert outsider.poll() is None and listener_of('127.0.0.1',number)[0]
    finally:
        outsider.terminate()
        outsider.wait(timeout=10)
