"""request_user_input merged into the agent-nexus inbox (decision D11, design §8.12).

With a manager whose gateway announced ``ask/1``, questions become gateway
asks; the tool names, parameters and result shapes stay the same. Without one
(or when it fails) the tools fall back to the chatx window. The manager here
is a fake object: no real manager, gateway or chatx is involved.
"""
import threading
import time

import pytest

from conftest import device_server
from test_ask import FakeCompanion, call
from src.manager_asks import ManagerAsks, as_question


class FakeManagerAsks:
    def __init__(self, available: bool = True) -> None:
        self.is_available = available
        self.asks: dict[str, dict] = {}
        self.created: list[dict] = []
        self._lock = threading.Lock()

    def available(self) -> bool:
        return self.is_available

    def create(self, question, *, title, kind, options, allow_custom):
        with self._lock:
            ask_id = f"q_{len(self.asks) + 1:026d}"
            self.asks[ask_id] = {"ask_id": ask_id, "state": "open", "answer": None, "options": options or []}
            self.created.append({"question": question, "title": title, "kind": kind, "options": options,
                                 "allow_custom": allow_custom})
        return {"ask_id": ask_id, "message_id": "m_x", "state": "open"}

    def state(self, ask_id):
        with self._lock:
            if ask_id not in self.asks:
                raise RuntimeError(f"unknown ask {ask_id!r}")
            return dict(self.asks[ask_id])

    def answer(self, ask_id, **answer):
        with self._lock:
            self.asks[ask_id].update(state="answered", answer={**answer, "via": "toast"})


@pytest.fixture
def companion():
    fake = FakeCompanion()
    try:
        yield fake
    finally:
        fake.close()


@pytest.fixture
def server(tmp_path, companion):
    instance = device_server(tmp_path, companion_url=companion.url, ask_poll_interval_s=0.05, ask_timeout_s=5.0)
    instance.manager_asks = FakeManagerAsks()
    return instance


def answer_soon(fake: FakeManagerAsks, **answer) -> None:
    def worker():
        deadline = time.time() + 4
        while time.time() < deadline:
            if fake.asks:
                fake.answer(next(iter(fake.asks)), **answer)
                return
            time.sleep(0.02)
    threading.Thread(target=worker, daemon=True).start()


@pytest.mark.asyncio
async def test_request_user_input_goes_to_the_inbox_and_keeps_its_result_shape(server, companion):
    answer_soon(server.manager_asks, choice="b")
    result = await call(server, "request_user_input", question="部署到哪里？", kind="single", options=["a", "b"])
    payload = result.structured_content
    assert payload == {"status": "ok", "question_id": payload["question_id"], "answered": True,
                       "selected": ["b"], "text": None}
    assert payload["question_id"].startswith("q_")
    assert server.manager_asks.created[0]["options"] == ["a", "b"]
    assert companion.posted == []  # chatx was not used


@pytest.mark.asyncio
async def test_async_and_result_poll_the_gateway(server):
    posted = (await call(server, "request_user_input_async", question="有什么补充？")).structured_content
    assert posted["answered"] is False and posted["question_id"].startswith("q_")
    pending = (await call(server, "request_user_input_result", question_id=posted["question_id"])).structured_content
    assert pending["pending"] is True and "inbox" in pending["message"]
    server.manager_asks.answer(posted["question_id"], text="先跑一周")
    done = (await call(server, "request_user_input_result", question_id=posted["question_id"])).structured_content
    assert done["answered"] is True and done["text"] == "先跑一周" and done["selected"] == []


@pytest.mark.asyncio
async def test_without_a_gateway_the_chatx_window_is_used(server, companion):
    server.manager_asks.is_available = False
    posted = (await call(server, "request_user_input_async", question="在吗？")).structured_content
    assert not posted["question_id"].startswith("q_")
    assert companion.posted and not server.manager_asks.created


def test_manager_url_must_be_loopback_and_answers_map_to_the_tool_shape(monkeypatch):
    monkeypatch.setenv("ANX_MANAGER_URL", "http://example.invalid:8399")
    assert ManagerAsks.from_env() is None
    monkeypatch.setenv("ANX_MANAGER_URL", "http://127.0.0.1:9")
    assert ManagerAsks.from_env().url == "http://127.0.0.1:9"
    assert ManagerAsks("http://127.0.0.1:9").available() is False  # nothing listens there
    question, status = as_question({"ask_id": "q_1", "state": "answered", "answer": {"choices": ["x", "y"]}})
    assert status == "answered" and question["answer"]["selected"] == ["x", "y"]
    assert as_question({"ask_id": "q_1", "state": "expired"})[1] == "cancelled"
