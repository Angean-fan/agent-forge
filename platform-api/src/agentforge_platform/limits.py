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


DEFAULT_FACTORY_MAX_TOOL_OPS = 120
FACTORY_MAX_TOOL_OPS_ENV = "AGENTFORGE_FACTORY_MAX_TOOL_OPS"
DEFAULT_FACTORY_CODER_MAX_TOOL_OPS = 60
FACTORY_CODER_MAX_TOOL_OPS_ENV = "AGENTFORGE_FACTORY_CODER_MAX_TOOL_OPS"
DEFAULT_FACTORY_TESTER_MAX_TOOL_OPS = 50
FACTORY_TESTER_MAX_TOOL_OPS_ENV = "AGENTFORGE_FACTORY_TESTER_MAX_TOOL_OPS"


def runtime_limits() -> RuntimeLimits:
    values = {
        field: int(os.environ.get(_ENV[field], str(_DEFAULTS[field])))
        for field in _DEFAULTS
    }
    return RuntimeLimits(**values)


def factory_max_tool_ops() -> int:
    """工厂多部门子 Agent（DeptCoder / DeptTester）的工具操作上限。

    优先级：``AGENTFORGE_FACTORY_MAX_TOOL_OPS`` > ``AGENTFORGE_MAX_TOOL_OPS`` > 默认 120。
    保持既有 R21 ``runtime_limits().max_tool_ops`` 默认值（40）不变。
    """
    raw = os.environ.get(FACTORY_MAX_TOOL_OPS_ENV, "").strip()
    if raw:
        return max(1, int(raw))
    global_raw = os.environ.get(_ENV["max_tool_ops"], "").strip()
    if global_raw:
        return max(1, int(global_raw))
    return DEFAULT_FACTORY_MAX_TOOL_OPS


def factory_role_max_tool_ops(role: str) -> int:
    """按子 Agent 最小职责收紧单角色工具操作上限（防止单子 Agent 上下文失控膨胀）。

    优先级：角色专属 env（``AGENTFORGE_FACTORY_CODER_MAX_TOOL_OPS`` / ``AGENTFORGE_FACTORY_TESTER_MAX_TOOL_OPS``）
    > ``AGENTFORGE_FACTORY_MAX_TOOL_OPS`` > 角色最小职责默认值（coder=24, tester=16）。
    """
    if role == "coder":
        role_env, role_default = FACTORY_CODER_MAX_TOOL_OPS_ENV, DEFAULT_FACTORY_CODER_MAX_TOOL_OPS
    elif role == "tester":
        role_env, role_default = FACTORY_TESTER_MAX_TOOL_OPS_ENV, DEFAULT_FACTORY_TESTER_MAX_TOOL_OPS
    else:
        return factory_max_tool_ops()

    raw_role = os.environ.get(role_env, "").strip()
    if raw_role:
        return max(1, int(raw_role))
    raw_factory = os.environ.get(FACTORY_MAX_TOOL_OPS_ENV, "").strip()
    if raw_factory:
        return max(1, int(raw_factory))
    return role_default


def factory_agent_recursion_limit(role: str | None = None) -> int:
    """工厂子 Agent 的 LangGraph 递归步数上限（每工具操作≈2 步 + 余量 5）。"""
    ops = factory_role_max_tool_ops(role) if role else factory_max_tool_ops()
    return 2 * ops + 5


def model_timeout_seconds() -> int:
    return runtime_limits().model_timeout_seconds


__all__ = [
    "DEFAULT_FACTORY_MAX_TOOL_OPS",
    "FACTORY_MAX_TOOL_OPS_ENV",
    "RuntimeLimits",
    "factory_agent_recursion_limit",
    "factory_max_tool_ops",
    "model_timeout_seconds",
    "runtime_limits",
]
