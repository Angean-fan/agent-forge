"""M23 生成图在 verify 阶段调用受控 Runner。

默认 HTTP 客户端；测试可注入实现同一 `RunnerClient` 协议的假对象。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol


class RunnerClient(Protocol):
    def verify(self, artifact_dir: str) -> dict: ...

    def accept(self, artifact_dir: str) -> dict: ...


class HttpRunnerClient:
    def __init__(self, *, base_url: str, token: str, timeout: int = 1800) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def _post(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("X-Runner-Token", self._token)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Runner HTTP {error.code}") from None
        except (urllib.error.URLError, OSError) as error:
            raise RuntimeError(f"Runner 连接失败：{type(error).__name__}") from None

    def verify(self, artifact_dir: str) -> dict:
        return self._post("/verify", {"artifact_dir": artifact_dir})

    def accept(self, artifact_dir: str) -> dict:
        return self._post("/accept", {"artifact_dir": artifact_dir})
