"""request_user_input, against a stand-in for the chatx companion.

These tests never talk to the real companion: a tiny HTTP server plays the part
of chatx's ``/ask`` endpoints, so the answer arrives on a thread exactly the way
a person clicking a button would deliver it.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from conftest import device_server
from src.server import DeviceServer


class FakeCompanion:
    """Accepts questions and lets a test answer them, like the chatx UI."""

    def __init__(self) -> None:
        self.questions: dict[str, dict] = {}
        self.posted: list[dict] = []
        self._lock = threading.Lock()
        self._seq = 0

        companion = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # keep pytest output clean
                pass

            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                incoming = json.loads(self.rfile.read(length) or b"{}")
                question = companion.create(incoming)
                self._send(200, {"status": "ok", "questionStatus": "pending", "question": question})

            def do_GET(self) -> None:
                query = parse_qs(urlparse(self.path).query)
                question_id = (query.get("id") or [""])[0]
                question = companion.get(question_id)
                if question is None:
                    self._send(404, {"status": "error", "error": f"unknown question id: {question_id}"})
                    return
                self._send(200, {
                    "status": "ok",
                    "questionStatus": companion.status(question),
                    "question": question,
                })

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def create(self, incoming: dict) -> dict:
        with self._lock:
            self._seq += 1
            question = {
                "id": f"ask-{self._seq}",
                "title": incoming.get("title"),
                "question": incoming["question"],
                "kind": incoming.get("kind", "text"),
                "options": incoming.get("options", []),
                "allowCustom": incoming.get("allowCustom", False),
                "source": incoming.get("source", "mcp"),
                "createdAtMs": 0,
                "answeredAtMs": None,
                "answer": None,
                "cancelled": False,
            }
            self.questions[question["id"]] = question
            self.posted.append(incoming)
            return question

    def get(self, question_id: str) -> dict | None:
        with self._lock:
            return self.questions.get(question_id)

    @staticmethod
    def status(question: dict) -> str:
        if question.get("answer"):
            return "answered"
        return "cancelled" if question.get("cancelled") else "pending"

    def answer(self, question_id: str, selected: list[str], text: str | None = None) -> None:
        with self._lock:
            self.questions[question_id]["answer"] = {"selected": selected, "text": text}

    def skip(self, question_id: str) -> None:
        with self._lock:
            self.questions[question_id]["cancelled"] = True

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def companion():
    fake = FakeCompanion()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def server(tmp_path: Path, companion: FakeCompanion) -> DeviceServer:
    return device_server(
        tmp_path,
        companion_url=companion.url,
        watch_notify_timeout_s=2.0,
        ask_poll_interval_s=0.05,
        ask_timeout_s=5.0,
    )


async def call(server: DeviceServer, name: str, **arguments):
    tool = await server.mcp.get_tool(name)
    return await tool.run(arguments)


def answer_soon(companion: FakeCompanion, selected: list[str], text: str | None = None) -> None:
    """Answer the first question to arrive, the way a person would: a bit late."""
    def worker() -> None:
        deadline = time.time() + 4.0
        while time.time() < deadline:
            ids = list(companion.questions)
            if ids:
                companion.answer(ids[0], selected, text)
                return
            time.sleep(0.02)

    threading.Thread(target=worker, daemon=True).start()


@pytest.mark.asyncio
async def test_blocks_until_the_person_answers(server: DeviceServer, companion: FakeCompanion) -> None:
    answer_soon(companion, ["b"])
    result = await call(
        server, "request_user_input",
        question="部署到哪里？", kind="single", options=["a", "b"],
    )
    payload = result.structured_content
    assert payload["answered"] is True
    assert payload["selected"] == ["b"]
    assert payload["question_id"]


@pytest.mark.asyncio
async def test_free_text_answer_comes_back_as_text(server: DeviceServer, companion: FakeCompanion) -> None:
    answer_soon(companion, [], "先跑一周再说")
    result = await call(server, "request_user_input", question="有什么补充？")
    payload = result.structured_content
    assert payload["answered"] is True
    assert payload["text"] == "先跑一周再说"
    assert payload["selected"] == []
    # A text question always offers its box, without the caller asking.
    assert companion.posted[0]["kind"] == "text"


@pytest.mark.asyncio
async def test_timeout_returns_a_question_id_rather_than_an_error(server: DeviceServer) -> None:
    """Nobody is at the machine. The caller must be able to come back for it."""
    result = await call(
        server, "request_user_input", question="在吗？", timeout_s=1.0,
    )
    payload = result.structured_content
    assert payload["status"] == "ok"
    assert payload["answered"] is False
    assert payload["pending"] is True
    assert payload["question_id"]
    assert "request_user_input_result" in payload["message"]


@pytest.mark.asyncio
async def test_a_pending_answer_can_be_collected_later(
    server: DeviceServer, companion: FakeCompanion
) -> None:
    posted = await call(server, "request_user_input_async", question="选一个", kind="multi",
                        options=["x", "y", "z"])
    question_id = posted.structured_content["question_id"]
    assert posted.structured_content["answered"] is False

    pending = await call(server, "request_user_input_result", question_id=question_id)
    assert pending.structured_content["answered"] is False

    companion.answer(question_id, ["x", "z"])
    collected = await call(server, "request_user_input_result", question_id=question_id)
    assert collected.structured_content["answered"] is True
    assert collected.structured_content["selected"] == ["x", "z"]


@pytest.mark.asyncio
async def test_skipping_is_reported_as_skipped_not_as_an_empty_answer(
    server: DeviceServer, companion: FakeCompanion
) -> None:
    posted = await call(server, "request_user_input_async", question="要现在发吗？",
                        kind="single", options=["发", "先别"])
    question_id = posted.structured_content["question_id"]
    companion.skip(question_id)

    result = await call(server, "request_user_input_result", question_id=question_id)
    payload = result.structured_content
    assert payload["answered"] is False
    assert payload["cancelled"] is True
    assert payload["selected"] == []
    assert "skipped" in payload["message"]


@pytest.mark.asyncio
async def test_bad_arguments_are_refused_before_bothering_the_person(
    server: DeviceServer, companion: FakeCompanion
) -> None:
    for arguments in (
        {"question": "   "},
        {"question": "选一个", "kind": "single"},                       # no options
        {"question": "选一个", "kind": "menu", "options": ["a"]},        # unknown kind
        {"question": "选一个", "kind": "multi", "options": ["a", "a"]},  # duplicates
    ):
        result = await call(server, "request_user_input", **arguments)
        assert result.structured_content["status"] == "error", arguments
    assert companion.posted == []


@pytest.mark.asyncio
async def test_a_missing_companion_says_so_instead_of_hanging(tmp_path: Path) -> None:
    offline = device_server(tmp_path, companion_url="http://127.0.0.1:9",
                           watch_notify_timeout_s=0.2, ask_timeout_s=2.0)
    result = await call(offline, "request_user_input", question="在吗？")
    payload = result.structured_content
    assert payload["status"] == "error"
    assert "chatx" in payload["error"]
