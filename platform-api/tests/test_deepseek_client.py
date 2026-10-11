"""DeepSeek 请求参数与响应元数据边界。"""

from __future__ import annotations

import io
import json

from agentforge_platform.clients import DeepSeekClient


def test_json_request_limits_output_and_uses_body_request_id(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")

    class Response(io.BytesIO):
        def __init__(self, data):
            super().__init__(data)
            self.headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(request, timeout):
        captured["payload"] = json.loads(request.data)
        captured["timeout"] = timeout
        return Response(
            json.dumps(
                {
                    "id": "body-request-id",
                    "model": "deepseek-flash",
                    "choices": [{"message": {"content": '{"ok":true}'}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 8},
                }
            ).encode()
        )

    monkeypatch.setattr("agentforge_platform.clients.urllib.request.urlopen", fake_urlopen)
    result = DeepSeekClient().complete(
        model="deepseek-flash",
        prompt="只输出 JSON",
        thinking_mode="disabled",
        json_mode=True,
        max_output_tokens=512,
    )

    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert captured["payload"]["max_tokens"] == 512
    assert result.request_id == "body-request-id"
    assert result.usage.input_tokens == 12
