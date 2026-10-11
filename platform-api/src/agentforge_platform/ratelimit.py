"""M28 登录限流：进程内滑动窗口。

MVP 平台为单 API 进程，进程内计数即可；若将来横向扩展为多进程/多实例，
须替换为共享存储（如 Redis）实现，否则各进程各自计数。

计数对象为**客户端 IP**；窗口与阈值可经环境变量配置。
"""

from __future__ import annotations

import os
import time
from collections import deque
from collections.abc import Callable


def login_max_attempts() -> int:
    return int(os.environ.get("AGENTFORGE_LOGIN_MAX_ATTEMPTS", "10"))


def login_window_seconds() -> float:
    return float(os.environ.get("AGENTFORGE_LOGIN_WINDOW_SECONDS", "60"))


class SlidingWindowLimiter:
    """按 key 计数：窗口内达到上限即拒绝；过期的历史计数在读取时淘汰。"""

    def __init__(
        self,
        max_attempts: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max = max(1, int(max_attempts))
        self._window = max(0.0, float(window_seconds))
        self._clock = clock
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str) -> bool:
        """记录一次尝试并返回是否放行（放行时计入）。"""
        now = self._clock()
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] >= self._window:
            bucket.popleft()
        if len(bucket) >= self._max:
            return False
        bucket.append(now)
        return True
