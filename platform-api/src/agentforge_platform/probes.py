"""C01：模型路由**就绪状态**报告（**只读、不联网、不付费**）。

打印生产路由在每个角色上的判定（开关 / 凭据 / 是否已授权可用 / 原因），用于：

- 审计"无意外跨厂商调用"：**默认全关**，未就绪角色一律 ``enabled=False`` 且不构造客户端；
- 在启用前确认地域端点、模型名与缺凭据原因（``DISABLED`` / ``CONFIG_REQUIRED``）。

**真实受限工具调用探针**（会真实计费）不在本模块，见既有的 ``scripts/coding_tools_probe.py``
（R16 阶段 A/B：地域 Function Calling + 受限引擎端到端+逐次入账），或 ``scripts/deepseek_thinking_probe.py``。

用法::

    uv run python -m agentforge_platform.probes
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from agentforge_platform.db import SessionLocal
from agentforge_platform.production_routing import build_production_routing

REAL_PROBE_HINTS = (
    "真实 coding 工具探针（计费）：`python ../scripts/coding_tools_probe.py`（需 DASHSCOPE_API_KEY）",
    "真实 DeepSeek 思考探针（计费）：`python ../scripts/deepseek_thinking_probe.py`",
)


def readiness_report(*, work_root: str = "./var/runs") -> dict[str, Any]:
    """构造生产路由并返回**脱敏**就绪报告（不发起任何外部请求）。"""
    routing = build_production_routing(session_factory=SessionLocal, work_root=work_root)
    return {
        "any_enabled": routing.any_enabled(),
        "roles": routing.status_dict(),
        "injected": {
            "planning_client": routing.planning_client is not None,
            "review_client": routing.review_client is not None,
            "coding_routing": routing.coding_routing is not None,
        },
        "real_probe_hints": list(REAL_PROBE_HINTS),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="C01 模型路由就绪状态（只读、不联网）")
    parser.add_argument("--work-root", default="./var/runs")
    args = parser.parse_args(argv)
    print(json.dumps(readiness_report(work_root=args.work_root), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
