"""A fake model provider on 127.0.0.1 speaking the three streaming formats of docs/agent-chat.md §16.4.

    server = FakeApiServer().start()
    server.script(reply(text="hi", tool_calls=[call("read_file", {"path": "a.txt"})]), reply(text="done"))
    ... base_url = server.url ...
    server.requests  # [{"method", "path", "headers", "body"}]
    server.stop()

The protocol is told by the path: ``/chat/completions`` (openai), ``/v1/messages`` (anthropic),
``:streamGenerateContent`` (gemini), ``GET .../models`` (model lists). Each request takes the next scripted step
(default: the text "ok"): a reply (text / thinking / tool calls / usage, streamed in small pieces), an HTTP error
(``status``, ``body``, ``headers`` e.g. Retry-After), or ``hang`` (stream a little, then wait).
No real provider is ever contacted.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def call(name: str, args: dict, call_id: str | None = None) -> dict:
    return {"name": name, "args": args, "id": call_id}


def reply(text: str = "", *, thinking: str = "", tool_calls: list[dict] | None = None, usage: tuple[int, int] = (100, 20),
          signature: str = "sig-1") -> dict:
    return {"text": text, "thinking": thinking, "tool_calls": tool_calls or [], "usage": usage, "signature": signature}


def error(status: int, message: str = "nope", headers: dict | None = None) -> dict:
    return {"status": status, "body": {"error": {"message": message, "type": "error"}}, "headers": headers or {}}


def _halves(text: str) -> list[str]:
    if len(text) < 2:
        return [text] if text else []
    mid = len(text) // 2
    return [text[:mid], text[mid:]]


class FakeApiServer:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.steps: list[dict] = []
        self.models = [{"id": "fake-small", "owned_by": "fake", "context_length": 32000}, {"id": "fake-large"}]
        self.lock = threading.Lock()
        self.release = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args: Any) -> None:  # quiet
                pass

            def _record(self, body: Any) -> None:
                with server.lock:
                    server.requests.append({"method": self.command, "path": self.path,
                                            "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})

            def do_GET(self) -> None:
                self._record(None)
                step = server._next(default=None)
                if step and step.get("status"):
                    return self._error(step)
                if "/v1beta/" in self.path:
                    data = {"models": [{"name": f"models/{m['id']}", "displayName": m["id"],
                                        "supportedGenerationMethods": ["generateContent"]} for m in server.models]}
                else:
                    data = {"object": "list", "data": server.models}
                self._json(200, data)

            def do_POST(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    body = None
                self._record(body)
                step = server._next(default=reply("ok"))
                if step.get("status"):
                    return self._error(step)
                protocol = "anthropic" if self.path.split("?")[0].endswith("/messages") else \
                    "gemini" if "GenerateContent" in self.path else "openai"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    for event, data in getattr(server, f"_{protocol}")(step, body or {}):
                        if event:
                            self.wfile.write(f"event: {event}\n".encode())
                        payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                    if step.get("hang"):
                        server.release.wait(step["hang"])
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, status: int, data: Any, headers: dict | None = None) -> None:
                raw = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def _error(self, step: dict) -> None:
                self._json(step["status"], step.get("body") or {}, step.get("headers"))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    # -- control
    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> FakeApiServer:
        self.thread.start()
        return self

    def stop(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()

    def script(self, *steps: dict) -> None:
        with self.lock:
            self.steps.extend(steps)

    def _next(self, default: dict | None) -> dict | None:
        with self.lock:
            return self.steps.pop(0) if self.steps else default

    def posts(self) -> list[dict]:
        with self.lock:
            return [r for r in self.requests if r["method"] == "POST"]

    # -- the formats
    @staticmethod
    def _openai(step: dict, body: dict):
        model = body.get("model", "fake")
        base = {"id": "chatcmpl-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}

        def chunk(delta: dict, finish: str | None = None) -> dict:
            return {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        yield "", chunk({"role": "assistant", "content": ""})
        for piece in _halves(step.get("thinking", "")):
            yield "", chunk({"reasoning_content": piece})
        for piece in _halves(step.get("text", "")):
            yield "", chunk({"content": piece})
        for index, tc in enumerate(step.get("tool_calls") or []):
            args = json.dumps(tc["args"])
            first, second = args[: len(args) // 2], args[len(args) // 2:]
            yield "", chunk({"tool_calls": [{"index": index, "id": tc.get("id") or f"call_{index}", "type": "function",
                                              "function": {"name": tc["name"], "arguments": first}}]})
            yield "", chunk({"tool_calls": [{"index": index, "function": {"arguments": second}}]})
        if step.get("hang"):
            return
        yield "", chunk({}, "tool_calls" if step.get("tool_calls") else "stop")
        prompt, completion = step.get("usage", (100, 20))
        yield "", {**base, "choices": [], "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                                                    "total_tokens": prompt + completion,
                                                    "prompt_tokens_details": {"cached_tokens": 10}}}
        yield "", "[DONE]"

    @staticmethod
    def _anthropic(step: dict, body: dict):
        prompt, completion = step.get("usage", (100, 20))
        yield "message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": body.get("model", "fake"), "content": [],
            "usage": {"input_tokens": prompt - 30, "cache_read_input_tokens": 30, "output_tokens": 1}}}
        yield "ping", {"type": "ping"}
        index = 0
        if step.get("thinking"):
            yield "content_block_start", {"type": "content_block_start", "index": index,
                                          "content_block": {"type": "thinking", "thinking": ""}}
            for piece in _halves(step["thinking"]):
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "thinking_delta", "thinking": piece}}
            yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                          "delta": {"type": "signature_delta", "signature": step.get("signature", "")}}
            yield "content_block_stop", {"type": "content_block_stop", "index": index}
            index += 1
        if step.get("text"):
            yield "content_block_start", {"type": "content_block_start", "index": index,
                                          "content_block": {"type": "text", "text": ""}}
            for piece in _halves(step["text"]):
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "text_delta", "text": piece}}
            yield "content_block_stop", {"type": "content_block_stop", "index": index}
            index += 1
        for n, tc in enumerate(step.get("tool_calls") or []):
            args = json.dumps(tc["args"])
            yield "content_block_start", {"type": "content_block_start", "index": index, "content_block": {
                "type": "tool_use", "id": tc.get("id") or f"toolu_{n}", "name": tc["name"], "input": {}}}
            for piece in _halves(args):
                yield "content_block_delta", {"type": "content_block_delta", "index": index,
                                              "delta": {"type": "input_json_delta", "partial_json": piece}}
            yield "content_block_stop", {"type": "content_block_stop", "index": index}
            index += 1
        if step.get("hang"):
            return
        yield "message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": "tool_use" if step.get("tool_calls") else "end_turn"},
                                "usage": {"output_tokens": completion}}
        yield "message_stop", {"type": "message_stop"}

    @staticmethod
    def _gemini(step: dict, body: dict):
        def part_chunk(parts: list[dict]) -> dict:
            return {"candidates": [{"content": {"role": "model", "parts": parts}, "index": 0}],
                    "modelVersion": "fake-gemini"}

        for piece in _halves(step.get("thinking", "")):
            yield "", part_chunk([{"text": piece, "thought": True}])
        for piece in _halves(step.get("text", "")):
            yield "", part_chunk([{"text": piece}])
        calls = [{"functionCall": {"name": tc["name"], "args": tc["args"]}, "thoughtSignature": f"ts-{n}"}
                 for n, tc in enumerate(step.get("tool_calls") or [])]
        if calls:
            yield "", part_chunk(calls)
        if step.get("hang"):
            return
        prompt, completion = step.get("usage", (100, 20))
        yield "", {"candidates": [{"content": {"role": "model", "parts": [{"text": ""}]}, "finishReason": "STOP",
                                   "index": 0}],
                   "usageMetadata": {"promptTokenCount": prompt, "candidatesTokenCount": completion,
                                     "totalTokenCount": prompt + completion}, "modelVersion": "fake-gemini"}


__all__ = ["FakeApiServer", "call", "error", "reply"]
