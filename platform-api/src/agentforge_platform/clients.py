"""模型 Provider 客户端接口与 DeepSeek 官方实现。

密钥**只从环境变量读取**，不写入代码、配置、日志或产物。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Completion:
    content: str
    model: str
    request_id: str | None
    usage: Usage | None
    finish_reason: str | None = None


class ModelClient(Protocol):
    def complete(
        self, *, model: str, prompt: str, thinking_mode: str | None = None
    ) -> Completion: ...


class DeepSeekClient:
    """DeepSeek 官方 API 客户端（OpenAI 兼容 chat/completions）。"""

    def __init__(
        self, *, api_key_env: str = "DEEPSEEK_API_KEY", base_url: str = "https://api.deepseek.com"
    ) -> None:
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")

    def complete(
        self, *, model: str, prompt: str, thinking_mode: str | None = None
    ) -> Completion:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise RuntimeError(f"环境变量 {self._api_key_env} 未配置")

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if thinking_mode:
            payload["thinking"] = {"type": thinking_mode}

        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
                request_id = response.headers.get("x-request-id")
        except urllib.error.HTTPError as error:
            # 不回显响应体（可能含敏感信息）
            raise RuntimeError(f"DeepSeek API HTTP {error.code}") from None

        choices = body.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        raw_usage = body.get("usage") or {}
        usage = (
            Usage(int(raw_usage["prompt_tokens"]), int(raw_usage["completion_tokens"]))
            if {"prompt_tokens", "completion_tokens"} <= raw_usage.keys()
            else None
        )
        return Completion(
            content=str(message.get("content", "")),
            model=str(body.get("model", model)),
            request_id=request_id,
            usage=usage,
            finish_reason=choices[0].get("finish_reason") if choices else None,
        )
