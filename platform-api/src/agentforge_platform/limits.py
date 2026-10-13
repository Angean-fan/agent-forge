"""R21：统一运行护栏——全 run 模型次数、单次输出、模型等待、Agent 工具循环与运行总时限。

默认值取技术文档第 8 节建议：30 次模型调用 / 12,000 Token 输出 / 120 秒等待 / 40 次工具操作 / 20 分钟总时限；
均可经环境变量收紧或调整。**所有角色**（DeepSeek 交互、Qwen 规划/编码/升级修复）共用同一组限制。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULTS = {
    "max_model_calls_per_run": 30,
    "max_output_tokens_per_call": 12_000,
    "model_timeout_seconds": 120,
    "max_tool_ops": 40,
    "run_timeout_seconds": 20 * 60,
}
_ENV = {
    "max_model_calls_per_run": "AGENTFORGE_MAX_MODEL_CALLS_PER_RUN",
    "max_output_tokens_per_call": "AGENTFORGE_MAX_OUTPUT_TOKENS",
    "model_timeout_seconds": "AGENTFORGE_MODEL_TIMEOUT_SECONDS",
    "max_tool_ops": "AGENTFORGE_MAX_TOOL_OPS",
    "run_timeout_seconds": "AGENTFORGE_RUN_TIMEOUT_SECONDS",
}


@dataclass(frozen=True)
class RuntimeLimits:
    max_model_calls_per_run: int = _DEFAULTS["max_model_calls_per_run"]
    max_output_tokens_per_call: int = _DEFAULTS["max_output_tokens_per_call"]
    model_timeout_seconds: int = _DEFAULTS["model_timeout_seconds"]
    max_tool_ops: int = _DEFAULTS["max_tool_ops"]
    run_timeout_seconds: int = _DEFAULTS["run_timeout_seconds"]

    @property
    def agent_recursion_limit(self) -> int:
        """LangGraph 递归上限：每次工具操作 ≈ 模型步 + 工具步，另留少量余量。"""
        return 2 * self.max_tool_ops + 5


def runtime_limits() -> RuntimeLimits:
    values = {
        field: int(os.environ.get(_ENV[field], str(_DEFAULTS[field])))
        for field in _DEFAULTS
    }
    return RuntimeLimits(**values)


def model_timeout_seconds() -> int:
    return runtime_limits().model_timeout_seconds


__all__ = ["RuntimeLimits", "model_timeout_seconds", "runtime_limits"]
