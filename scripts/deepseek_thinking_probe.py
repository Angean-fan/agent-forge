"""R03 DeepSeek 思考模式探针（**会真实计费**，默认不随门禁运行，需人工显式执行）。

用途：核实 DeepSeek 官方 API 对 `thinking.type=disabled/enabled` 与 `reasoning_effort=low/high`
的实际支持情况与响应形态，作为启用对应模式的依据。输出脱敏证据（不打印密钥、不保存隐藏思考）。

用法（需 DEEPSEEK_API_KEY；本机 TLS 需要时 `SSL_CERT_FILE=/etc/ssl/cert.pem`）：

    SSL_CERT_FILE=/etc/ssl/cert.pem DEEPSEEK_API_KEY=... \
        uv run --locked python ../scripts/deepseek_thinking_probe.py

退出码：0 = 全部请求成功返回；非 0 = 存在网络/鉴权/参数错误（证据仍打印，供归档）。
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
MODEL = "deepseek-flash"

# (label, thinking_mode, reasoning_effort)
CASES: list[tuple[str, str | None, str | None]] = [
    ("default_none", None, None),
    ("disabled", "disabled", None),
    ("enabled_no_effort", "enabled", None),
    ("enabled_low", "enabled", "low"),
    ("enabled_high", "enabled", "high"),
]
PROMPT = "用一句话回答：报名和活动是什么关系？只输出 JSON {\"answer\": \"...\"}。"


def _probe(api_key: str, label: str, thinking: str | None, effort: str | None) -> bool:
    payload: dict[str, object] = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "max_tokens": 128,
        "response_format": {"type": "json_object"},
    }
    if thinking:
        payload["thinking"] = {"type": thinking}
    if effort is not None:
        payload["reasoning_effort"] = effort

    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key}")

    evidence: dict[str, object] = {
        "case": label,
        "request_thinking": payload.get("thinking"),
        "request_reasoning_effort": payload.get("reasoning_effort"),
    }
    last_error: str | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = json.loads(response.read().decode("utf-8"))
            message = (body.get("choices") or [{}])[0].get("message", {})
            evidence.update({
                "http_status": 200,
                "response_model": body.get("model"),
                "usage": body.get("usage"),
                "has_reasoning_content": "reasoning_content" in message,
                "content_present": bool(message.get("content")),
                "attempts": attempt + 1,
            })
            print(json.dumps(evidence, ensure_ascii=False))
            return True
        except urllib.error.HTTPError as error:
            # 参数/鉴权类错误不重试；记录后返回。
            evidence.update({"http_status": error.code, "error": error.reason})
            print(json.dumps(evidence, ensure_ascii=False))
            return False
        except (urllib.error.URLError, http.client.RemoteDisconnected, ConnectionError) as exc:
            last_error = type(exc).__name__  # 瞬时网络抖动 → 退避重试
            time.sleep(0.8 * (attempt + 1))
        except Exception as exc:
            last_error = type(exc).__name__
            break
    evidence.update({"error": last_error, "attempts": 3})
    print(json.dumps(evidence, ensure_ascii=False))
    return False


def main() -> int:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        print("环境变量 DEEPSEEK_API_KEY 未配置", file=sys.stderr)
        return 2
    print(f"# base_url={BASE_URL} model={MODEL} ssl_cert_file={os.environ.get('SSL_CERT_FILE')}")
    results = [_probe(api_key, *case) for case in CASES]
    print(f"# summary: {sum(results)}/{len(results)} ok")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
