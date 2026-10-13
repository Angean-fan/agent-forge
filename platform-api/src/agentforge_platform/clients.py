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

from agentforge_platform.limits import model_timeout_seconds


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
        self,
        *,
        model: str,
        prompt: str,
        thinking_mode: str | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Completion: ...


class ProviderPreflightError(RuntimeError):
    """The provider request was definitely not sent."""


class DeepSeekClient:
    """DeepSeek 官方 API 客户端（OpenAI 兼容 chat/completions）。"""

    def __init__(
        self, *, api_key_env: str = "DEEPSEEK_API_KEY", base_url: str = "https://api.deepseek.com"
    ) -> None:
        self._api_key_env = api_key_env
        self._base_url = base_url.rstrip("/")

    @property
    def timeout_seconds(self) -> int:
        """R21：模型等待时限（默认 120s，可经 AGENTFORGE_MODEL_TIMEOUT_SECONDS 调整）。"""
        return model_timeout_seconds()

    def complete(
        self,
        *,
        model: str,
        prompt: str,
        thinking_mode: str | None = None,
        json_mode: bool = False,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> Completion:
        api_key = os.environ.get(self._api_key_env)
        if not api_key:
            raise ProviderPreflightError(f"环境变量 {self._api_key_env} 未配置")

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        if thinking_mode:
            payload["thinking"] = {"type": thinking_mode}
        # 思考强度独立于 thinking.type 传递；仅在思考模式下有效（调用方/网关负责组合校验）。
        if reasoning_effort is not None:
            if thinking_mode != "enabled":
                raise ValueError("reasoning_effort 仅适用于 thinking.type=enabled")
            payload["reasoning_effort"] = reasoning_effort
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_output_tokens is not None:
            payload["max_tokens"] = max_output_tokens

        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
                request_id = response.headers.get("x-request-id") or body.get("id")
        except urllib.error.HTTPError as error:
            # 不回显响应体（可能含敏感信息）
            raise RuntimeError(f"DeepSeek API HTTP {error.code}") from None

        choices = body.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        # 仅取最终 content；思考模式返回的 reasoning_content 属隐藏思考，不读取、不落账、不回显。
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


class DashScopeClient(DeepSeekClient):
    """阿里云百炼（DashScope）OpenAI 兼容模式客户端（如 Qwen3.7 Plus）。

    与 DeepSeek 客户端同协议，仅默认环境变量与 base_url 不同；密钥同样只从环境变量读取、不落日志。
    """

    def __init__(
        self,
        *,
        api_key_env: str = "DASHSCOPE_API_KEY",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ) -> None:
        super().__init__(api_key_env=api_key_env, base_url=base_url)
