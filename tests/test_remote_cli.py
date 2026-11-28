"""agent_nexus.remote_cli without a network: the MCP client (JSON and SSE answers, degraded states), result parsing,
argument parsing and the exit codes. End to end with a real gateway and devices: test_remote_cli_e2e.py."""
from __future__ import annotations

import io
import json

import httpx
import pytest

from agent_nexus import remote_cli as R


class FakeMcp:
    """An MCP server for httpx.MockTransport: ``tools`` maps a tool name to fn(arguments) -> result dict."""

    def __init__(self, tools: dict, *, sse: bool = False, degraded: str | None = None):
        self.tools = tools
        self.sse = sse
        self.degraded = degraded
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/cloud":
            return httpx.Response(200, json={"agent_id": "me", "joined": True, "hostname": "me-host"})
        body = json.loads(request.content)
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "t", "version": "1"}}
            if self.degraded:
                result["_meta"] = {"anx/state": self.degraded, "anx/reason": f"原因：{self.degraded}"}
            return self.answer(body["id"], result)
        assert method == "tools/call"
        name, arguments = body["params"]["name"], body["params"]["arguments"]
        if name == "tooluse":
            name, arguments = arguments["tool"], {**arguments["arguments"], "_mcp_id": arguments["mcp_id"]}
        self.calls.append((request.url.path, name, arguments))
        return self.answer(body["id"], self.tools[name](arguments))

    def answer(self, rid, result) -> httpx.Response:
        message = {"jsonrpc": "2.0", "id": rid, "result": result}
        if not self.sse:
            return httpx.Response(200, json=message)
        text = ('event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                f"event: message\ndata: {json.dumps(message)}\n\n")
        return httpx.Response(200, content=text.encode(), headers={"content-type": "text/event-stream"})


def gateway_exec(text: str, **fields) -> dict:
    """An exec result as the gateway presents it (gateway.exec_text + _meta)."""
    return {"content": [{"type": "text", "text": text}, {"type": "text", "text": "[device: nas]"}],
            "_meta": {"anx/exec": fields}, "isError": False}


def listing(*rows) -> dict:
    return {"content": [{"type": "text", "text": "{}"}], "structuredContent": {"devices": list(rows)},
            "isError": False}


NAS = {"device_id": "nas-1", "display_name": "NAS", "online": True, "shells": ["bash", "sh"], "shell_default": "bash",
       "workdirs": [{"name": "default", "path": "/srv"}]}


@pytest.fixture
def mock(monkeypatch):
    def install(server):
        monkeypatch.setattr(R, "_http", lambda: httpx.Client(transport=httpx.MockTransport(server)))
        return server
    return install


def run(*argv: str, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = R.main(list(argv), None, stdout=out, stderr=err, stdin=io.StringIO(stdin),
                  env={"ANX_CONSOLE_URL": "http://127.0.0.1:8399"})
    return code, out.getvalue(), err.getvalue()


@pytest.mark.parametrize("sse", [False, True])
def test_exec_loops_over_waits_streams_output_and_returns_the_exit_code(mock, sse, monkeypatch):
    monkeypatch.setattr(R, "WAIT_STEPS_MS", (10, 10, 10, 10))
    waits = iter([gateway_exec("[running · session 5 · 1.0s · nas · bash] exec_wait(session_id=5) to continue\n"
                               "(no output)", session_id=5, original_token_count=0),
                  gateway_exec("[running · session 5 · 2.0s · nas · bash]\npart two\n", session_id=5,
                               original_token_count=3),
                  gateway_exec("[exit 3 · 3.0s · nas · bash]\nend\n", exit_code=3, original_token_count=1)])
    server = mock(FakeMcp({
        "device_list": lambda a: listing(NAS),
        "exec_command": lambda a: gateway_exec("[running · session 5 · 1.0s · nas · bash]\npart one\n", session_id=5,
                                               original_token_count=3),
        "exec_wait": lambda a: next(waits),
    }, sse=sse))
    code, out, err = run("exec", "nas", "--shell", "bash", "--", "make", "test")
    assert (code, out) == (3, "part one\npart two\nend\n"), err
    sent = next(args for path, name, args in server.calls if name == "exec_command")
    assert sent["cmd"] == "make test" and sent["stdin"] == "" and sent["workdir"] == "/srv" and sent["shell"] == "bash"
    assert sent["_mcp_id"] == "nas-1/device"
    assert all(args["timeout_ms"] <= R.WAIT_CAP_MS for _, name, args in server.calls if name == "exec_wait")


def test_this_computer_goes_to_mcp_self_with_raw_results(mock):
    server = mock(FakeMcp({"exec_command": lambda a: {
        "content": [{"type": "text", "text": "{}"}], "isError": False,
        "structuredContent": {"output": "local\n", "exit_code": 0, "original_token_count": 2}}}))
    assert run("exec", "--", "hostname") == (0, "local\n", "")
    assert server.calls[0][0] == "/mcp/self" and "workdir" not in server.calls[0][2]
    # this device's own id is "." too, without listing the devices
    assert run("exec", "me", "--", "hostname")[0] == 0
    assert all(path == "/mcp/self" for path, *_ in server.calls)


def test_detach_prints_only_the_token(mock):
    mock(FakeMcp({"device_list": lambda a: listing(NAS),
                  "exec_command": lambda a: gateway_exec("[running · session 9 · 0.2s · nas]\nearly\n", session_id=9,
                                                         original_token_count=1)}))
    code, out, err = run("exec", "NAS", "--detach", "--", "sleep 100")
    assert (code, out) == (0, "nas-1:9\n") and "early" in err


def test_timeout_leaves_the_command_running(mock, monkeypatch):
    monkeypatch.setattr(R, "WAIT_STEPS_MS", (10,))
    mock(FakeMcp({"device_list": lambda a: listing(NAS),
                  "exec_command": lambda a: gateway_exec("[running · session 2]\n(no output)", session_id=2),
                  "exec_wait": lambda a: gateway_exec("[running · session 2]\n(no output)", session_id=2)}))
    code, out, err = run("exec", "nas", "--timeout", "0.3", "--", "sleep 100")
    assert code == R.EXIT_TIMEOUT and out == "" and "anx wait nas-1:2" in err and "anx kill nas-1:2" in err


def test_unavailable_states_and_no_client(mock):
    mock(FakeMcp({}, degraded="locked"))
    code, _, err = run("exec", "nas", "--", "true")
    assert code == R.EXIT_UNAVAILABLE and "原因：locked" in err

    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    mock(refuse)
    code, _, err = run("devices")
    assert code == R.EXIT_NO_CLIENT and "没有运行" in err

    mock(lambda request: httpx.Response(403, text="forbidden: 这个账户不能使用本机 MCP"))
    code, _, err = run("exec", ".", "--", "true")
    assert code == R.EXIT_UNAVAILABLE and "这个账户" in err


def test_device_and_shell_resolution(mock):
    other = {**NAS, "device_id": "nas-2", "display_name": "nas"}
    off = {"device_id": "old", "display_name": "old", "online": False}
    mock(FakeMcp({"device_list": lambda a: listing(NAS, other, off)}))
    assert run("exec", "nas", "--", "true")[0] == R.EXIT_DEVICE  # two devices answer to "nas"
    code, _, err = run("exec", "old", "--", "true")
    assert code == R.EXIT_DEVICE and "不在线" in err
    code, _, err = run("exec", "nas-1", "--shell", "pwsh", "--", "true")
    assert code == R.EXIT_SHELL and "可用：bash, sh（默认 bash）" in err


def test_tool_errors_and_an_old_device_without_stdin(mock):
    def command(arguments):
        if "stdin" in arguments:
            return {"content": [{"type": "text", "text": "invalid tool arguments: Additional properties are not "
                                 "allowed ('stdin' was unexpected)"}], "isError": True}
        return gateway_exec("[exit 0 · nas]\nok\n", exit_code=0, original_token_count=1)
    server = mock(FakeMcp({"device_list": lambda a: listing(NAS), "exec_command": command,
                           "exec_wait": lambda a: {"content": [{"type": "text", "text":
                                                                "[error · nas] unknown exec session_id 4"}],
                                                   "_meta": {"anx/exec": {"status": "error",
                                                                          "error": "unknown exec session_id 4"}},
                                                   "isError": True}}))
    assert run("exec", "nas", "--", "true") == (0, "ok\n", "")
    assert [n for _, n, _ in server.calls].count("exec_command") == 2
    code, _, err = run("wait", "nas:4")
    assert code == R.EXIT_TOOL and "会话不存在或已过期" in err


def test_stdin_command_text_and_usage_errors(mock):
    server = mock(FakeMcp({"exec_command": lambda a: {"structuredContent": {"output": "", "exit_code": 0},
                                                      "content": [], "isError": False}}))
    text = "echo \"$HOME\" 'a b' `x`\n"
    assert run("exec", ".", "--stdin", stdin="\ufeff" + text)[0] == 0
    assert server.calls[-1][2]["cmd"] == text
    for argv in (["exec"], ["exec", "."], ["exec", "--bogus", "--", "x"], ["exec", ".", "--stdin", "--", "x"],
                 ["exec", "--timeout", "-1", "--", "x"], ["wait", "nas:abc"], ["cp", "nas", "b:/x"]):
        assert run(*argv)[0] == R.EXIT_USAGE, argv


def test_kill_prints_the_rest_and_cp_outcomes(mock, monkeypatch):
    states = {"completed": 0, "completed_with_errors": 3, "failed": 4, "cancelled": 5, "interrupted": 6}
    for state, expected in states.items():
        mock(FakeMcp({"device_list": lambda a: listing(NAS),
                      "file_copy": lambda a: {"structuredContent": {"job_id": "me:1", "state": "queued"},
                                              "content": [], "isError": False},
                      "file_copy_status": lambda a, state=state: {
                          "structuredContent": {"job_id": "me:1", "state": state, "files": {"done": 2},
                                                "bytes": {"done": 10}}, "content": [], "isError": False}}))
        code, out, err = run("cp", "nas:/srv/x", ".:/tmp/y")
        assert code == expected, (state, out, err)
        if state == "interrupted":
            assert "续传" in err
    server = mock(FakeMcp({"exec_kill": lambda a: {"structuredContent": {"output": "bye\n", "exit_code": -15},
                                                   "content": [], "isError": False}}))
    assert run("kill", ".:12")[:2] == (0, "bye\n")
    assert server.calls[-1][1:] == ("exec_kill", {"session_id": 12})


def test_result_parsing():
    assert R.exec_fields(gateway_exec("[exit 0 · 0.1s · nas]\n(no output)", exit_code=0,
                                      original_token_count=0))["output"] == ""
    # a command that really printed "(no output)" keeps it
    assert R.exec_fields(gateway_exec("[exit 0]\n(no output)", exit_code=0,
                                      original_token_count=3))["output"] == "(no output)"
    # header only (an older gateway without _meta)
    plain = {"content": [{"type": "text", "text": "[exit 2 · nas]\nboom\n"}], "isError": False}
    assert R.exec_fields(plain) == {"output": "boom\n", "exit_code": 2}
    running = {"content": [{"type": "text", "text": "[running · session 7 · 1s]\n(no output)"}]}
    assert R.exec_fields(running) == {"output": "", "session_id": 7}
    with pytest.raises(R.ToolFailure, match="policy denied"):
        R.exec_fields({"content": [{"type": "text", "text": "[error · nas] policy denied"}], "isError": True,
                       "_meta": {"anx/exec": {"status": "error", "error": "policy denied"}}})


@pytest.mark.parametrize("code,windows,expected", [(0, False, 0), (3, False, 3), (-9, False, 137), (256, False, 1),
                                                   (257, False, 1), (300, False, 44), (-1073741510, True, -1073741510),
                                                   (3221225786, True, -1073741510), (5, True, 5),
                                                   (None, False, 1)])
def test_exit_status(code, windows, expected):
    assert R.exit_status(code, windows=windows) == expected


def test_parsers_and_console_address():
    opts = R.parse_exec(["nas", "--shell", "pwsh", "--cwd=/x", "ls", "-la", "--shell"])
    assert opts["device"] == "nas" and opts["shell"] == "pwsh" and opts["cwd"] == "/x"
    assert opts["command"] == ["ls", "-la", "--shell"]  # like ssh: the rest is the command
    assert R.parse_exec(["--", "echo", "--detach"])["command"] == ["echo", "--detach"]
    assert R.parse_exec(["--timeout", "5", "--detach", "box", "--", "x"])["timeout"] == 5.0
    assert R.parse_token("nas:12") == ("nas", 12) and R.parse_token("12") == (".", 12)
    assert R.parse_token("host:with:colons:3") == ("host:with:colons", 3)
    assert R.parse_place("nas:C:\\data") == ("nas", "C:\\data")
    assert R.console_base(env={"ANX_CONSOLE_URL": "http://localhost:9000/"}) == "http://127.0.0.1:9000"
    with pytest.raises(R.CliError):
        R.console_base(env={"ANX_CONSOLE_URL": "http://example.com:80"})


def test_sse_waits_for_its_own_answer():
    lines = ['data: {"jsonrpc":"2.0","id":9,"result":{"x":0}}', "",
             'data: {"jsonrpc":"2.0",', 'data: "id":3,"result":{"x":1}}', ""]
    response = type("R", (), {"iter_lines": lambda self: iter(lines)})()
    assert R.Mcp._sse(response, 3)["result"] == {"x": 1}
    with pytest.raises(R.CliError) as broken:
        R.Mcp._sse(type("R", (), {"iter_lines": lambda self: iter(lines[:2])})(), 3)
    assert broken.value.transient


def running(session: int, text: str = "(no output)") -> dict:
    return gateway_exec(f"[running · session {session}]\n{text}", session_id=session,
                        original_token_count=0 if text == "(no output)" else 1)


def test_a_client_restart_mid_command_is_ridden_out(mock, monkeypatch):
    """Connection refused after the client answered once: retried (the client restarting or updating), with a
    note that output may be missing; past the grace the resume token, never "the client is not running"."""
    monkeypatch.setattr(R, "WAIT_STEPS_MS", (10,))
    monkeypatch.setattr(R, "RETRY_S", 0.01)
    down = {"left": 2}
    waits = iter([running(5, "one\n"), gateway_exec("[exit 0]\ntwo\n", exit_code=0, original_token_count=1)])
    inner = FakeMcp({"device_list": lambda a: listing(NAS), "exec_command": lambda a: running(5),
                     "exec_wait": lambda a: next(waits)})
    state = {"waits": 0}

    def server(request):
        if request.url.path == "/mcp" and b"exec_wait" in request.content:
            state["waits"] += 1
            if state["waits"] == 2:
                raise httpx.ReadError("reset", request=request)  # may have reached the device
            if state["waits"] > 2 and down["left"]:
                down["left"] -= 1
                raise httpx.ConnectError("refused", request=request)
        return inner(request)
    mock(server)
    code, out, err = run("exec", "nas", "--", "x")
    assert (code, out) == (0, "one\ntwo\n"), err
    assert "可能缺了一段输出" in err and "没有运行" not in err

    # the client never comes back: the token to resume with
    monkeypatch.setattr(R, "LOST_GRACE_S", 0.05)
    always = FakeMcp({"device_list": lambda a: listing(NAS), "exec_command": lambda a: running(6)})

    def gone(request):
        if b"exec_wait" in request.content:
            raise httpx.ConnectError("refused", request=request)
        return always(request)
    mock(gone)
    code, _, err = run("exec", "nas", "--", "x")
    assert code == R.EXIT_UNAVAILABLE and "anx wait nas-1:6" in err and "没有运行" not in err
    # but the first contact refused is still "not running"
    mock(lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused", request=request)))
    code, _, err = run("wait", "nas:6")
    assert code == R.EXIT_NO_CLIENT and "没有运行" in err


def test_cp_rides_out_an_unknown_status_while_the_runner_is_away(mock, monkeypatch):
    monkeypatch.setattr(R, "RETRY_S", 0.01)
    away = {"job_id": "pc:1", "state": "unknown", "runner_device": "pc",
            "message": "运行任务的设备离线；任务在设备上继续/中断，设备上线后可再查询"}
    snaps = iter([away, away, {"job_id": "pc:1", "state": "completed", "files": {"done": 1}}])

    def started(_a):
        return {"structuredContent": {"job_id": "pc:1", "state": "queued"}, "content": [], "isError": False}
    mock(FakeMcp({"device_list": lambda a: listing(NAS), "file_copy": started,
                  "file_copy_status": lambda a: {"structuredContent": next(snaps), "content": [], "isError": False}}))
    code, out, err = run("cp", "nas:/srv/x", ".:/tmp/y")
    assert code == 0 and out.startswith("完成") and err.count("设备离线") == 1, (out, err)

    # still away past the grace: unknown (6), the job id, and no invitation to start a second job right now
    monkeypatch.setattr(R, "LOST_GRACE_S", 0.05)
    mock(FakeMcp({"device_list": lambda a: listing(NAS), "file_copy": started,
                  "file_copy_status": lambda a: {"structuredContent": away, "content": [], "isError": False}}))
    code, out, err = run("cp", "nas:/srv/x", ".:/tmp/y")
    assert code == 6 and "pc:1" in err and "等它结束后" in err and "重新运行同一条命令即可续传\n" not in err
    # a status ask that failed in transit (a RoutingError): retried the same way
    failing = {"content": [{"type": "text", "text": "设备没有回应"}], "isError": True}
    mock(FakeMcp({"device_list": lambda a: listing(NAS), "file_copy": started,
                  "file_copy_status": lambda a: failing}))
    code, _, err = run("cp", "nas:/srv/x", ".:/tmp/y")
    assert code == R.EXIT_UNAVAILABLE and "pc:1" in err and "可能仍在后台运行" in err


class ClosedAfter(io.StringIO):
    """A stdout whose reader goes away after the first write (``| head -1``)."""

    def write(self, text):
        if self.getvalue():
            raise BrokenPipeError()
        return super().write(text)


def test_a_closed_stdout_stops_the_remote_command(mock, monkeypatch):
    monkeypatch.setattr(R, "WAIT_STEPS_MS", (10,))
    server = mock(FakeMcp({"device_list": lambda a: listing(NAS), "exec_command": lambda a: running(8, "line\n"),
                           "exec_wait": lambda a: running(8, "more\n"),
                           "exec_kill": lambda a: gateway_exec("[exit -15]\n(no output)", exit_code=-15)}))
    out, err = ClosedAfter(), io.StringIO()
    code = R.main(["exec", "nas", "--", "journalctl -f"], None, stdout=out, stderr=err, stdin=io.StringIO(),
                  env={"ANX_CONSOLE_URL": "http://127.0.0.1:8399"})
    assert code == R.EXIT_PIPE and out.getvalue() == "line\n"
    assert [n for _, n, _ in server.calls].count("exec_kill") == 1


def test_a_failed_kill_or_cancel_is_not_reported_as_done(mock, monkeypatch):
    import signal
    import threading
    offline = {"content": [{"type": "text", "text": "[error · nas] manager nas-1 is offline"}], "isError": True,
               "_meta": {"anx/exec": {"status": "error", "error": "manager nas-1 is offline"}}}
    mock(FakeMcp({"device_list": lambda a: listing(NAS), "exec_kill": lambda a: offline}))
    out, err = io.StringIO(), io.StringIO()
    ctx = R.Ctx("http://127.0.0.1:8399", R.Out(out), R.Out(err))
    execution = R.ExecRun(ctx.resolve("nas"), R.Out(out), R.Out(err))
    execution.session = 3
    execution._kill()
    assert "没能停止远程命令" in err.getvalue() and "anx kill nas-1:3" in err.getvalue()
    assert "已停止" not in err.getvalue()
    # a session that already ended is not a failure
    mock(FakeMcp({"device_list": lambda a: listing(NAS), "exec_kill": lambda a: {
        "content": [{"type": "text", "text": "[error · nas] unknown exec session_id 3"}], "isError": True,
        "_meta": {"anx/exec": {"status": "error", "error": "unknown exec session_id 3"}}}}))
    err.truncate(0)
    again = R.ExecRun(R.Ctx("http://127.0.0.1:8399", R.Out(out), R.Out(err)).resolve("nas"), R.Out(out), R.Out(err))
    again.session = 3
    again._kill()
    assert "已经结束" in err.getvalue() and "没能停止" not in err.getvalue()

    # anx cp: Ctrl+C while the runner cannot be reached to cancel
    monkeypatch.setattr(R, "CP_WAIT_MS", 10)
    mock(FakeMcp({"device_list": lambda a: listing(NAS),
                  "file_copy": lambda a: {"structuredContent": {"job_id": "pc:2", "state": "queued"}, "content": [],
                                          "isError": False},
                  "file_copy_status": lambda a: {"structuredContent": {"job_id": "pc:2", "state": "copying"},
                                                 "content": [], "isError": False},
                  "file_copy_cancel": lambda a: {"content": [{"type": "text", "text":
                                                              "运行任务的设备 pc 不在线，暂时无法取消"}],
                                                 "isError": True}}))
    threading.Timer(0.5, lambda: signal.raise_signal(signal.SIGINT)).start()
    code, _, err_text = run("cp", "nas:/srv/x", ".:/tmp/y")
    assert code == R.EXIT_INTERRUPTED and "取消失败" in err_text and "pc:2" in err_text, err_text
    assert "已取消复制" not in err_text


def test_mcp_switched_off_is_named_not_an_old_client(mock):
    def server(request):
        if request.url.path == "/api/mcp/local":
            return httpx.Response(200, json={"enabled": False, "state": "off"})
        return httpx.Response(404, text="not found")
    mock(server)
    code, _, err = run("exec", ".", "--", "true")
    assert code == R.EXIT_UNAVAILABLE and "本机 MCP 已关闭" in err and "版本过旧" not in err

    mock(lambda request: httpx.Response(404, text="not found"))  # a client without the route at all
    code, _, err = run("exec", ".", "--", "true")
    assert code == R.EXIT_UNAVAILABLE and "版本过旧" in err
