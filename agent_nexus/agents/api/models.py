"""Built-in context windows of common API models (§16.3: used when the provider config has no ``context_window``).

Matched by the longest prefix of the model id's last path segment (``openrouter`` ids look like
``anthropic/claude-sonnet-4.5``), lower case. Unknown models give None (the ring shows a token count instead).
"""
from __future__ import annotations

from typing import Any

#: model-id prefix -> context window (tokens)
CONTEXT_WINDOWS: dict[str, int] = {
    # OpenAI
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o4-mini": 200_000,
    "codex-mini": 200_000,
    # Anthropic
    "claude": 200_000,
    # Google
    "gemini-1.5-pro": 2_097_152,
    "gemini-1.5-flash": 1_048_576,
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5": 1_048_576,
    "gemini-3": 1_048_576,
    # DeepSeek
    "deepseek-chat": 128_000,
    "deepseek-reasoner": 128_000,
    "deepseek-v3": 128_000,
    "deepseek-r1": 128_000,
    # 通义
    "qwen-max": 32_768,
    "qwen-plus": 131_072,
    "qwen-turbo": 1_000_000,
    "qwen-long": 10_000_000,
    "qwen3-coder": 262_144,
    "qwen3": 131_072,
    "qwen2.5": 131_072,
    # Kimi
    "moonshot-v1-8k": 8_192,
    "moonshot-v1-32k": 32_768,
    "moonshot-v1-128k": 131_072,
    "kimi-k2": 262_144,
    "kimi-latest": 131_072,
    # 智谱
    "glm-4.6": 200_000,
    "glm-4.5": 128_000,
    "glm-4": 128_000,
    # open models (Ollama / vLLM)
    "llama3.1": 131_072,
    "llama3.2": 131_072,
    "llama3.3": 131_072,
    "mistral-large": 131_072,
}


def context_window(model: Any) -> int | None:
    """The built-in context window of ``model`` or None."""
    if not isinstance(model, str) or not model.strip():
        return None
    name = model.strip().lower().split("/")[-1].split(":")[0]
    if name.endswith("[1m]"):
        return 1_000_000
    best = max((prefix for prefix in CONTEXT_WINDOWS if name.startswith(prefix)), key=len, default=None)
    return CONTEXT_WINDOWS[best] if best else None


__all__ = ["CONTEXT_WINDOWS", "context_window"]
