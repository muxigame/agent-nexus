"""The three streaming wire formats of §16.4, behind one interface.

* ``openai``    -- Chat Completions: ``POST {base}/chat/completions`` with ``stream: true`` and
  ``stream_options.include_usage``; tools are function calling. ``reasoning_content`` (DeepSeek, 通义, Kimi) and
  ``reasoning`` (OpenRouter) deltas are thinking.
* ``anthropic`` -- Messages: ``POST {base}/v1/messages`` (SSE events ``message_start`` … ``message_stop``);
  ``tool_use`` / ``tool_result`` blocks, ``thinking`` blocks with their signature.
* ``gemini``    -- ``POST {base}/v1beta/models/{model}:streamGenerateContent?alt=sse``; ``functionDeclarations``
  / ``functionResponse``, ``thought`` parts, ``thoughtSignature`` kept for the next request.

The conversation is kept in a neutral form (what :mod:`.store` saves for resuming)::

    {"role": "user", "text": "..."}
    {"role": "assistant", "text": "...", "thinking": "...", "signature": "...",
     "tool_calls": [{"id": "call_1", "name": "read_file", "args": {...}, "signature": "..."}]}
    {"role": "tool", "results": [{"id": "call_1", "name": "read_file", "output": "...", "error": false}]}

Parsing is sans-IO (:class:`Parser` subclasses fed SSE events, testable without a network); :func:`stream_turn`
does the HTTP with ``httpx`` (``trust_env=True``: the device's proxy settings and CA bundle apply).
"""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_OUTPUT = 8192
ERROR_BODY_MAX = 4000
CONNECT_TIMEOUT_S = 20.0
READ_TIMEOUT_S = 300.0


class ApiError(Exception):
    """A request that failed: ``status`` (HTTP, or 0 for the network), ``code`` for the console, ``retry_after``."""

    def __init__(self, message: str, *, status: int = 0, code: str = "api_error", retry_after: float | None = None,
                 host: str = ""):
        super().__init__(message)
        self.status, self.code, self.retry_after, self.host = status, code, retry_after, host


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    signature: str = ""  # gemini thoughtSignature

    def neutral(self) -> dict:
        out = {"id": self.id, "name": self.name, "args": self.args}
        if self.signature:
            out["signature"] = self.signature
        return out


@dataclass
class TurnResult:
    text: str = ""
    thinking: str = ""
    signature: str = ""  # anthropic thinking signature
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)  # {input, output, cached}
    stop_reason: str = ""
    model: str = ""

    def message(self) -> dict:
        out: dict[str, Any] = {"role": "assistant", "text": self.text}
        if self.thinking:
            out["thinking"] = self.thinking
        if self.signature:
            out["signature"] = self.signature
        if self.tool_calls:
            out["tool_calls"] = [call.neutral() for call in self.tool_calls]
        return out


#: (kind "text" | "thinking", text)
Delta = Callable[[str, str], None]


def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _args(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            value = json.loads(raw)
        except ValueError:
            return {"_raw": raw}
        return value if isinstance(value, dict) else {"_value": value}
    return {}


def host_of(url: str) -> str:
    """``host[:port]`` of ``url`` for messages -- never the userinfo (``user:secret@``) a URL may carry."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port
    except ValueError:
        return "服务商"
    if not host:
        return "服务商"
    host = f"[{host}]" if ":" in host else host
    return f"{host}:{port}" if port else host


def call_id() -> str:
    """An id for a tool call the provider gave none (unique in the session, valid for every protocol)."""
    return f"call_{uuid.uuid4().hex[:16]}"


# ============================================================================================== parsers

class Parser:
    """Fed one SSE event at a time (``event`` name, decoded JSON ``data``); collects the turn's result."""

    def __init__(self, on_delta: Delta | None = None):
        self.result = TurnResult()
        self.on_delta = on_delta or (lambda kind, text: None)
        self.done = False

    def _text(self, text: str) -> None:
        if text:
            self.result.text += text
            self.on_delta("text", text)

    def _thinking(self, text: str) -> None:
        if text:
            self.result.thinking += text
            self.on_delta("thinking", text)

    def feed(self, event: str, data: Any) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def finish(self) -> TurnResult:
        return self.result


class OpenAIParser(Parser):
    def __init__(self, on_delta: Delta | None = None):
        super().__init__(on_delta)
        self._calls: dict[int, dict] = {}

    def feed(self, event: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if isinstance(data.get("error"), dict):
            error = data["error"]
            raise ApiError(str(error.get("message") or error), code="stream_error")
        if isinstance(data.get("model"), str):
            self.result.model = data["model"]
        usage = data.get("usage")
        if isinstance(usage, dict):
            details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
            self.result.usage = {"input": _int(usage.get("prompt_tokens")), "output": _int(usage.get("completion_tokens")),
                                 "cached": _int(details.get("cached_tokens")) or _int(usage.get("prompt_cache_hit_tokens"))}
        for choice in data.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
            for key in ("reasoning_content", "reasoning"):
                if isinstance(delta.get(key), str):
                    self._thinking(delta[key])
            if isinstance(delta.get("content"), str):
                self._text(delta["content"])
            for call in delta.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                index = call.get("index") if isinstance(call.get("index"), int) else len(self._calls)
                slot = self._calls.setdefault(index, {"id": "", "name": "", "args": ""})
                if call.get("id"):
                    slot["id"] = str(call["id"])
                function = call.get("function") if isinstance(call.get("function"), dict) else {}
                if function.get("name") and not slot["name"]:
                    slot["name"] = str(function["name"])
                if isinstance(function.get("arguments"), str):
                    slot["args"] += function["arguments"]
            if choice.get("finish_reason"):
                self.result.stop_reason = str(choice["finish_reason"])

    def finish(self) -> TurnResult:
        for index in sorted(self._calls):
            slot = self._calls[index]
            if slot["name"]:
                self.result.tool_calls.append(ToolCall(slot["id"] or call_id(), slot["name"], _args(slot["args"])))
        return self.result


class AnthropicParser(Parser):
    def __init__(self, on_delta: Delta | None = None):
        super().__init__(on_delta)
        self._blocks: dict[int, dict] = {}

    def feed(self, event: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        kind = data.get("type") or event
        if kind == "error":
            error = data.get("error") if isinstance(data.get("error"), dict) else {}
            code = "overloaded" if error.get("type") == "overloaded_error" else "stream_error"
            raise ApiError(str(error.get("message") or "服务商返回了错误"), code=code)
        if kind == "message_start":
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            if isinstance(message.get("model"), str):
                self.result.model = message["model"]
            self._usage(message.get("usage"))
        elif kind == "content_block_start":
            index = data.get("index") if isinstance(data.get("index"), int) else len(self._blocks)
            block = data.get("content_block") if isinstance(data.get("content_block"), dict) else {}
            self._blocks[index] = {"type": block.get("type"), "id": block.get("id"), "name": block.get("name"),
                                   "json": "", "input": block.get("input")}
            if block.get("type") == "text":
                self._text(str(block.get("text") or ""))
            elif block.get("type") == "thinking":
                self._thinking(str(block.get("thinking") or ""))
        elif kind == "content_block_delta":
            index = data.get("index") if isinstance(data.get("index"), int) else 0
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            slot = self._blocks.setdefault(index, {"type": None, "json": ""})
            dtype = delta.get("type")
            if dtype == "text_delta":
                self._text(str(delta.get("text") or ""))
            elif dtype == "thinking_delta":
                self._thinking(str(delta.get("thinking") or ""))
            elif dtype == "signature_delta":
                self.result.signature += str(delta.get("signature") or "")
            elif dtype == "input_json_delta":
                slot["json"] = slot.get("json", "") + str(delta.get("partial_json") or "")
        elif kind == "message_delta":
            delta = data.get("delta") if isinstance(data.get("delta"), dict) else {}
            if delta.get("stop_reason"):
                self.result.stop_reason = str(delta["stop_reason"])
            self._usage(data.get("usage"))
        elif kind == "message_stop":
            self.done = True

    def _usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            return
        current = self.result.usage or {"input": 0, "output": 0, "cached": 0}
        if any(key in usage for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")):
            current["input"] = (_int(usage.get("input_tokens")) + _int(usage.get("cache_read_input_tokens"))
                                + _int(usage.get("cache_creation_input_tokens"))) or current["input"]
            current["cached"] = _int(usage.get("cache_read_input_tokens")) or current.get("cached", 0)
        if "output_tokens" in usage:
            current["output"] = _int(usage.get("output_tokens"))
        self.result.usage = current

    def finish(self) -> TurnResult:
        for index in sorted(self._blocks):
            block = self._blocks[index]
            if block.get("type") == "tool_use" and block.get("name"):
                args = _args(block["json"]) if block.get("json") else _args(block.get("input"))
                self.result.tool_calls.append(ToolCall(str(block.get("id") or call_id()), str(block["name"]), args))
        return self.result


class GeminiParser(Parser):
    def feed(self, event: str, data: Any) -> None:
        if isinstance(data, list):  # a JSON array stream (no alt=sse) carries the same objects
            for item in data:
                self.feed(event, item)
            return
        if not isinstance(data, dict):
            return
        if isinstance(data.get("error"), dict):
            raise ApiError(str(data["error"].get("message") or data["error"]), code="stream_error")
        if isinstance(data.get("modelVersion"), str):
            self.result.model = data["modelVersion"]
        usage = data.get("usageMetadata")
        if isinstance(usage, dict):
            self.result.usage = {"input": _int(usage.get("promptTokenCount")),
                                 "output": _int(usage.get("candidatesTokenCount")) + _int(usage.get("thoughtsTokenCount")),
                                 "cached": _int(usage.get("cachedContentTokenCount"))}
        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
            for part in content.get("parts") or []:
                if not isinstance(part, dict):
                    continue
                if isinstance(part.get("functionCall"), dict):
                    call = part["functionCall"]
                    # Gemini rarely sends an id (its functionResponse matches by name): one unique in the session
                    self.result.tool_calls.append(ToolCall(str(call.get("id") or call_id()),
                                                           str(call.get("name") or ""), _args(call.get("args")),
                                                           str(part.get("thoughtSignature") or "")))
                elif isinstance(part.get("text"), str):
                    if part.get("thought"):
                        self._thinking(part["text"])
                    else:
                        self._text(part["text"])
            if candidate.get("finishReason"):
                self.result.stop_reason = str(candidate["finishReason"])
        feedback = data.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise ApiError(f"请求被服务商拦截：{feedback['blockReason']}", code="blocked")


# ============================================================================================== requests

def _join(base: str, suffix: str, version: str) -> str:
    """``base`` + ``suffix``, without doubling a version segment the base already ends with."""
    base = base.rstrip("/")
    if version and base.endswith("/" + version) and suffix.startswith("/" + version + "/"):
        suffix = suffix[len(version) + 1:]
    return base + suffix


def endpoint(provider: dict, model_id: str, *, stream: bool = True) -> str:
    protocol, base = provider["protocol"], provider["base_url"]
    if protocol == "anthropic":
        return _join(base, "/v1/messages", "v1")
    if protocol == "gemini":
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return _join(base, f"/v1beta/models/{quote(model_id, safe='')}:{action}", "v1beta")
    return _join(base, "/chat/completions", "")


def models_endpoint(provider: dict) -> str:
    protocol, base = provider["protocol"], provider["base_url"]
    if protocol == "anthropic":
        return _join(base, "/v1/models", "v1")
    if protocol == "gemini":
        return _join(base, "/v1beta/models", "v1beta")
    return _join(base, "/models", "")


def headers(provider: dict, key: str | None) -> dict[str, str]:
    out = {"Content-Type": "application/json", "Accept": "text/event-stream", "User-Agent": "agent-nexus"}
    out.update(provider.get("headers") or {})
    protocol = provider["protocol"]
    if protocol == "anthropic":
        out["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            out["x-api-key"] = key
    elif protocol == "gemini":
        if key:
            out["x-goog-api-key"] = key
    elif key:
        out["Authorization"] = f"Bearer {key}"
    return out


def _tool_output(result: dict) -> str:
    return str(result.get("output") or "")


def _empty_assistant(msg: dict) -> bool:
    """An assistant turn with nothing to send (a stop after thinking only, a step that returned nothing): the
    providers refuse empty text blocks, so such turns are left out of the request."""
    return msg.get("role") == "assistant" and not msg.get("text") and not msg.get("tool_calls")


def _merge_roles(items: list[dict], key: str) -> list[dict]:
    """Consecutive entries of one role as one (tool results then the next user text; a left-out empty turn)."""
    out: list[dict] = []
    for item in items:
        if out and out[-1]["role"] == item["role"]:
            out[-1] = {**out[-1], key: [*out[-1][key], *item[key]]}
        else:
            out.append(item)
    return out


def openai_body(model: dict, system: str, history: list[dict], tools: list[dict], *, stream: bool = True,
                max_output: int | None = None) -> dict:
    messages: list[dict] = [{"role": "system", "content": system}] if system else []
    for msg in history:
        role = msg.get("role")
        if role == "user":
            messages.append({"role": "user", "content": msg.get("text", "")})
        elif role == "assistant":
            if _empty_assistant(msg):
                continue
            item: dict[str, Any] = {"role": "assistant", "content": msg.get("text") or None}
            if msg.get("tool_calls"):
                item["tool_calls"] = [{"id": c["id"], "type": "function",
                                       "function": {"name": c["name"], "arguments": json.dumps(c.get("args") or {},
                                                                                               ensure_ascii=False)}}
                                      for c in msg["tool_calls"]]
                if msg.get("thinking"):
                    # DeepSeek / Kimi thinking models want their reasoning back within a tool-call round
                    item["reasoning_content"] = msg["thinking"]
            messages.append(item)
        elif role == "tool":
            for result in msg.get("results") or []:
                messages.append({"role": "tool", "tool_call_id": result["id"], "content": _tool_output(result)})
    body: dict[str, Any] = {"model": model["id"], "messages": messages, "stream": stream}
    if stream:
        body["stream_options"] = {"include_usage": True}
    if tools:
        body["tools"] = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                           "parameters": t["parameters"]}} for t in tools]
    limit = max_output or model.get("max_output")
    if limit:
        reasoning = model["id"].split("/")[-1].startswith(("o1", "o3", "o4", "gpt-5"))
        body["max_completion_tokens" if reasoning else "max_tokens"] = int(limit)
    return body


def anthropic_body(model: dict, system: str, history: list[dict], tools: list[dict], *, stream: bool = True,
                   max_output: int | None = None) -> dict:
    messages: list[dict] = []
    for msg in history:
        role = msg.get("role")
        if role == "user":
            messages.append({"role": "user", "content": [{"type": "text", "text": msg.get("text", "")}]})
        elif role == "assistant":
            if _empty_assistant(msg):
                continue
            content: list[dict] = []
            if msg.get("thinking") and msg.get("signature"):
                content.append({"type": "thinking", "thinking": msg["thinking"], "signature": msg["signature"]})
            if msg.get("text"):
                content.append({"type": "text", "text": msg["text"]})
            for call in msg.get("tool_calls") or []:
                content.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call.get("args") or {}})
            messages.append({"role": "assistant", "content": content})
        elif role == "tool":
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": r["id"], "content": _tool_output(r), "is_error": bool(r.get("error"))}
                for r in msg.get("results") or []]})
    messages = _merge_roles(messages, "content")
    limit = int(max_output or model.get("max_output") or DEFAULT_MAX_OUTPUT)
    body: dict[str, Any] = {"model": model["id"], "max_tokens": limit, "messages": messages, "stream": stream}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                         for t in tools]
    if model.get("thinking") and limit > 2048:
        body["thinking"] = {"type": "enabled", "budget_tokens": max(1024, min(limit // 2, 16000))}
    return body


def _gemini_schema(schema: Any) -> Any:
    """JSON schema in the subset Gemini accepts (no ``additionalProperties``)."""
    if isinstance(schema, dict):
        return {k: _gemini_schema(v) for k, v in schema.items() if k not in ("additionalProperties", "$schema")}
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


def gemini_body(model: dict, system: str, history: list[dict], tools: list[dict], *, stream: bool = True,
                max_output: int | None = None) -> dict:
    contents: list[dict] = []
    for msg in history:
        role = msg.get("role")
        if role == "user":
            contents.append({"role": "user", "parts": [{"text": msg.get("text", "")}]})
        elif role == "assistant":
            if _empty_assistant(msg):
                continue
            parts: list[dict] = []
            if msg.get("text"):
                parts.append({"text": msg["text"]})
            for call in msg.get("tool_calls") or []:
                part: dict[str, Any] = {"functionCall": {"name": call["name"], "args": call.get("args") or {}}}
                if call.get("signature"):
                    part["thoughtSignature"] = call["signature"]
                parts.append(part)
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            contents.append({"role": "user", "parts": [
                {"functionResponse": {"name": r.get("name") or "tool",
                                      "response": {"error": _tool_output(r)} if r.get("error") else {"output": _tool_output(r)}}}
                for r in msg.get("results") or []]})
    body: dict[str, Any] = {"contents": _merge_roles(contents, "parts")}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = [{"functionDeclarations": [{"name": t["name"], "description": t["description"],
                                                    "parameters": _gemini_schema(t["parameters"])} for t in tools]}]
    config: dict[str, Any] = {}
    limit = max_output or model.get("max_output")
    if limit:
        config["maxOutputTokens"] = int(limit)
    if model.get("thinking"):
        config["thinkingConfig"] = {"includeThoughts": True}
    if config:
        body["generationConfig"] = config
    return body


BODIES = {"openai": openai_body, "anthropic": anthropic_body, "gemini": gemini_body}
PARSERS = {"openai": OpenAIParser, "anthropic": AnthropicParser, "gemini": GeminiParser}


# ============================================================================================== transport

def client(timeout: float | None = None) -> httpx.AsyncClient:
    """A client honouring the device's proxy settings and CA bundle (``trust_env``)."""
    read = timeout or READ_TIMEOUT_S
    return httpx.AsyncClient(trust_env=True, follow_redirects=False,
                             timeout=httpx.Timeout(read, connect=min(CONNECT_TIMEOUT_S, read)))


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


def _error_message(body: bytes) -> str:
    text = body[:ERROR_BODY_MAX].decode("utf-8", "replace").strip()
    try:
        data = json.loads(text)
    except ValueError:
        return text[:500]
    if isinstance(data, list) and data:
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if isinstance(error, str):
            return error[:500]
        if data.get("message"):
            return str(data["message"])[:500]
    return text[:500]


def http_error(status: int, body: bytes, host: str, retry_after: float | None = None) -> ApiError:
    """§16.4's error mapping: 401/403 = the key, 429 = rate limited (retried once), others as the provider says."""
    detail = _error_message(body)
    if status in (401, 403):
        return ApiError(f"密钥无效或无权限（{host} 返回 HTTP {status}）" + (f"：{detail}" if detail else ""),
                        status=status, code="auth", host=host)
    if status == 429:
        return ApiError(f"服务商限流（{host} 返回 HTTP 429）" + (f"：{detail}" if detail else ""), status=status,
                        code="rate_limited", retry_after=retry_after, host=host)
    if status == 404:
        return ApiError(f"{host} 返回 HTTP 404：地址或模型名不对" + (f"（{detail}）" if detail else ""), status=status,
                        code="not_found", host=host)
    return ApiError(f"{host} 返回 HTTP {status}" + (f"：{detail}" if detail else ""), status=status,
                    code="overloaded" if status in (503, 529) else "api_error", host=host)


def network_error(exc: Exception, host: str) -> ApiError:
    if isinstance(exc, httpx.TimeoutException):
        return ApiError(f"连接服务商 {host} 超时", code="timeout", host=host)
    reason = str(exc) or type(exc).__name__
    return ApiError(f"连不上服务商 {host}：{reason}", code="network", host=host)


async def sse_events(response: httpx.Response) -> AsyncIterator[tuple[str, Any]]:
    """``(event, data)`` of a ``text/event-stream`` body; ``data: [DONE]`` ends it."""
    event, data_lines = "", []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                data_lines = []
                if raw.strip() == "[DONE]":
                    return
                try:
                    yield event, json.loads(raw)
                except ValueError:
                    pass
            event = ""
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data_lines.append(value)
    if data_lines:
        raw = "\n".join(data_lines)
        if raw.strip() != "[DONE]":
            try:
                yield event, json.loads(raw)
            except ValueError:
                pass


async def stream_turn(http: httpx.AsyncClient, provider: dict, model: dict, key: str | None, system: str,
                      history: list[dict], tools: list[dict], on_delta: Delta | None = None,
                      *, max_output: int | None = None) -> TurnResult:
    """One streamed request; raises :class:`ApiError`."""
    protocol = provider["protocol"]
    url = endpoint(provider, model["id"])
    host = host_of(url)
    body = BODIES[protocol](model, system, history, tools, max_output=max_output)
    parser = PARSERS[protocol](on_delta)
    try:
        async with http.stream("POST", url, headers=headers(provider, key), json=body) as response:
            if response.status_code >= 400:
                raw = b""
                async for chunk in response.aiter_bytes():
                    raw += chunk
                    if len(raw) > ERROR_BODY_MAX:
                        break
                raise http_error(response.status_code, raw, host, _retry_after(response))
            content_type = response.headers.get("content-type", "")
            if "event-stream" not in content_type and "json" in content_type:
                # a provider that ignored stream: true answers with one JSON object (or a Gemini array)
                raw = await response.aread()
                try:
                    data = json.loads(raw)
                except ValueError as exc:
                    raise ApiError(f"{host} 的回答不是有效的 JSON", code="bad_response", host=host) from exc
                if protocol == "openai" and isinstance(data, dict):
                    data = _openai_whole(data)
                elif protocol == "anthropic" and isinstance(data, dict):
                    for event, item in _anthropic_whole(data):
                        parser.feed(event, item)
                    return parser.finish()
                parser.feed("", data)
                return parser.finish()
            async for event, data in sse_events(response):
                parser.feed(event, data)
    except httpx.HTTPError as exc:
        raise network_error(exc, host) from exc
    return parser.finish()


def _openai_whole(data: dict) -> dict:
    """A non-streamed chat completion as one delta chunk."""
    choices = []
    for choice in data.get("choices") or []:
        if isinstance(choice, dict):
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            calls = [{**call, "index": i} for i, call in enumerate(message.get("tool_calls") or []) if isinstance(call, dict)]
            choices.append({"delta": {**message, "tool_calls": calls}, "finish_reason": choice.get("finish_reason")})
    return {**data, "choices": choices}


def _anthropic_whole(data: dict) -> list[tuple[str, dict]]:
    events = [("message_start", {"type": "message_start", "message": {"model": data.get("model"), "usage": data.get("usage")}})]
    for index, block in enumerate(data.get("content") or []):
        if isinstance(block, dict):
            events.append(("content_block_start", {"type": "content_block_start", "index": index, "content_block": block}))
    events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": data.get("stop_reason")},
                                     "usage": {"output_tokens": (data.get("usage") or {}).get("output_tokens")}}))
    return events


async def list_models(http: httpx.AsyncClient, provider: dict, key: str | None) -> list[dict]:
    """The provider's model list: ``[{id, label?, context_window?, owned_by?}]``. Raises :class:`ApiError`."""
    url = models_endpoint(provider)
    host = host_of(url)
    head = {k: v for k, v in headers(provider, key).items() if k not in ("Content-Type", "Accept")}
    try:
        response = await http.get(url, headers=head)
    except httpx.HTTPError as exc:
        raise network_error(exc, host) from exc
    if response.status_code >= 400:
        raise http_error(response.status_code, response.content, host, _retry_after(response))
    try:
        data = response.json()
    except ValueError as exc:
        raise ApiError(f"{host} 的回答不是有效的 JSON", code="bad_response", host=host) from exc
    out: list[dict] = []
    items = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), list) else \
        data.get("models") if isinstance(data, dict) and isinstance(data.get("models"), list) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or item.get("name") or "")
        if provider["protocol"] == "gemini":
            model_id = model_id.removeprefix("models/")
            methods = item.get("supportedGenerationMethods")
            if isinstance(methods, list) and "generateContent" not in methods:
                continue
        if not model_id:
            continue
        entry: dict[str, Any] = {"id": model_id}
        label = item.get("display_name") or item.get("displayName")
        if isinstance(label, str) and label:
            entry["label"] = label
        window = item.get("context_length") or item.get("context_window") or item.get("inputTokenLimit")
        if isinstance(window, int) and window > 0:
            entry["context_window"] = window
        if isinstance(item.get("owned_by"), str):
            entry["owned_by"] = item["owned_by"]
        out.append(entry)
    return out


__all__ = [
    "AnthropicParser",
    "ApiError",
    "GeminiParser",
    "OpenAIParser",
    "ToolCall",
    "TurnResult",
    "anthropic_body",
    "client",
    "endpoint",
    "gemini_body",
    "headers",
    "host_of",
    "http_error",
    "list_models",
    "openai_body",
    "sse_events",
    "stream_turn",
]
