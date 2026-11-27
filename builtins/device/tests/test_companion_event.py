"""The companion wake adapter's endpoint (agent-nexus design §8.7, decision D12).

The local manager posts the wake prompt to ``POST /anx/companion-event``; the
plugin stores it as a watcher event (``watch_id='mail'``) and delivers it to
GPT Companion's ``POST /event`` exactly like a watcher would, so chatx needs
no change. GPT then calls ``watch_event(evt)`` and gets the prompt back. chatx
here is a fake HTTP server; no real chatx is contacted.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from starlette.testclient import TestClient

from conftest import device_server
from test_ask import call

PROMPT = ("[agent-nexus] 你（gpt-main@host-alpha）有 1 封新邮件，最新一封来自 alpha@host-alpha：「发版计划」。\n"
          "你的 workspace_id 是 abc（已续期）。请调用 mail_inbox(workspace_id=\"abc\") 阅读并处理。")


class FakeChatx:
    def __init__(self) -> None:
        self.events: list[dict] = []
        chatx = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                incoming = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                chatx.events.append({"path": self.path, **incoming})
                self._send({"status": "ok"})

            def do_GET(self) -> None:
                if self.path.startswith("/routes"):
                    self._send({"status": "ok", "routes": [{"id": "bm-auto", "workdir": "C:/work/project"}]})
                else:
                    self._send({"status": "ok", "currentUrl": None, "bookmark": None})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()


async def test_companion_event_reaches_chatx_and_watch_event_returns_the_prompt(tmp_path: Path) -> None:
    chatx = FakeChatx()
    server = device_server(tmp_path, companion_url=chatx.url, watch_notify_timeout_s=2.0)
    try:
        client = TestClient(server.mcp.http_app())
        refused = client.post("/anx/companion-event", json={"summary": PROMPT, "project_id": "bm-1"})
        assert refused.status_code == 403  # browsers cannot send X-ANX cross-origin
        assert client.post("/anx/companion-event", json={"project_id": "bm-1"},
                           headers={"X-ANX": "1"}).status_code == 400
        response = client.post("/anx/companion-event", headers={"X-ANX": "1"},
                               json={"project_id": "bm-1", "event_type": "mail_received", "summary": PROMPT,
                                     "workdir": str(tmp_path)})
        body = response.json()
        assert response.status_code == 200 and body["delivered"] is True and body["event_id"].startswith("evt")
        assert chatx.events == [{"path": "/event", "id": body["event_id"], "projectId": "bm-1",
                                 "eventType": "mail_received", "summary": PROMPT}]
        # What GPT does after chatx types "处理本地事件 evt-…".
        looked_up = await call(server, "watch_event", event_id=body["event_id"])
        payload = looked_up.structured_content
        assert payload["status"] == "ok" and payload["event"]["summary"] == PROMPT
        assert payload["event"]["watch_id"] == "mail" and payload["watch"] is None
        # companion_route "auto": the bookmark whose workdir matches.
        auto = client.post("/anx/companion-event", headers={"X-ANX": "1"},
                           json={"project_id": "auto", "summary": "x", "workdir": "C:/work/project"}).json()
        assert auto["delivered"] and auto["project_id"] == "bm-auto" and chatx.events[-1]["projectId"] == "bm-auto"
    finally:
        server.watch_manager.close()
        chatx.close()


def test_companion_event_without_chatx_is_not_delivered_and_not_retried_by_the_plugin(tmp_path: Path) -> None:
    server = device_server(tmp_path, watch_notify_timeout_s=0.2)  # companion_url points at a closed port
    try:
        client = TestClient(server.mcp.http_app())
        body = client.post("/anx/companion-event", headers={"X-ANX": "1"},
                           json={"project_id": "bm-1", "summary": PROMPT}).json()
        assert body["delivered"] is False and body["error"]
        event = server.watch_manager.event(body["event_id"])["event"]
        # The gateway retries the wake (with backoff); the plugin must not deliver it a second time.
        assert event["attempts"] >= 100
    finally:
        server.watch_manager.close()


def test_a_new_event_is_not_redelivered_by_the_retry_loop_while_its_first_delivery_runs(tmp_path: Path) -> None:
    # Seen on CI (Windows): the background _retry_pending picked up the row _emit had just inserted
    # (last_attempt_at was 0) and chatx got the same event twice.
    chatx = FakeChatx()
    server = device_server(tmp_path, companion_url=chatx.url)
    watches = server.watch_manager
    try:
        original = watches._deliver_event_id
        attempts: list[str] = []

        def deliver(event_id: str) -> None:
            attempts.append(event_id)
            if len(attempts) == 1:
                watches._retry_pending()  # what the worker thread may do while this first POST runs
                original(event_id)

        watches._deliver_event_id = deliver
        result = watches.emit_mail_event(project_id="bm-1", workdir=str(tmp_path), summary=PROMPT)
        assert result["delivered"] is True
        assert attempts == [result["event_id"]]  # the retry loop left the in-flight event alone
        assert [e["id"] for e in chatx.events] == [result["event_id"]]
    finally:
        chatx.close()
        server.watch_manager.close()
