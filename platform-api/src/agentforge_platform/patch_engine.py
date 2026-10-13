"""R16：DeepAgent 受限单任务 patch engine（coding 角色 = 百炼 ``qwen3-coder-flash``）。

设计要点（对齐技术文档第 1/6/9 节与 R16 约束）：

- **受限工具表**：只暴露 ``ls/read_file/write_file/edit_file/glob/grep`` 六个文件工具；
  ``execute``（shell）与 ``task``（未受控子代理委派）**显式移除**，并在构造后**审计真实工具表**再次确认，任一越权即拒绝构造。
- **受限 backend**：文件工具经 :class:`StagingBackend` 落到 R15 的 :class:`FileBackend`——
  读只见当前 run 的授权源码（叠加暂存区视图），写只进 run 专属暂存且受 ``allowed_paths``/受保护清单约束，**不碰正式源码**。
- **内部调用入账**：:class:`LedgerChatModel` 包装 ChatOpenAI，Agent 的**每一次**内部模型请求都继承
  ``run_id/task_id`` 并携带独立 ``call_id`` 走网关预留/结算，**不只统计外层 invoke**。
- **输出协议**：任务结束由程序（非模型）计算 :class:`FileChange` 变更集；模型输出不得直接登记为新资产。
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfileConfig,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends.protocol import (
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileData,
    FileInfo,
    GlobResult,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.utils import (
    InvalidGlobPatternError,
    compile_grep_include_glob,
    create_file_data,
    grep_matches_from_files,
    perform_string_replacement,
    slice_read_response,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError
from pydantic import ConfigDict, PrivateAttr
from sqlalchemy.orm import Session, sessionmaker

from agentforge_platform import model_gateway
from agentforge_platform.agent_files import STAGING_DIRNAME, FileBackend
from agentforge_platform.clients import ProviderPreflightError, Usage
from agentforge_platform.db import session_scope
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.limits import runtime_limits

DEFAULT_CODING_MODEL = "qwen3-coder-flash"
DEFAULT_DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
CODING_API_KEY_ENV = "DASHSCOPE_API_KEY"

# 允许暴露给 Agent 的文件工具（白名单）。删除（delete）不开放：缺口场景只做补全/修改，不删模块。
RESTRICTED_TOOLS: tuple[str, ...] = (
    "ls",
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
)
# 绝不能出现的工具：shell（任意命令）与子代理委派（未受控）。
FORBIDDEN_TOOL_NAMES: frozenset[str] = frozenset({"execute", "task"})

DEFAULT_MAX_INPUT_TOKENS = 60_000
DEFAULT_MAX_OUTPUT_TOKENS = 12_000


def coding_enabled() -> bool:
    """coding 角色仅在显式开启（R16 完成后）才可用；默认关闭。"""
    return os.environ.get("AGENTFORGE_CODING_ENABLED", "").strip().lower() in {"1", "true", "yes"}


def dashscope_base_url() -> str:
    """百炼地域/接口端点（可切换，如新加坡 intl 地域）。"""
    return os.environ.get("AGENTFORGE_DASHSCOPE_BASE_URL", DEFAULT_DASHSCOPE_BASE_URL).rstrip("/")


# --------------------------------------------------------------------------- #
# 受限文件后端：读叠加视图 + 写经 R15 暂存
# --------------------------------------------------------------------------- #
def _relative_path(path: str) -> str:
    """把 backend 的虚拟绝对路径（``/a/b``）规范为合法的 run 相对路径。"""
    if not isinstance(path, str) or not path.startswith("/"):
        raise AppException(ErrorCode.BAD_REQUEST, f"非法路径：{path!r}", status_code=422)
    relative = path.lstrip("/").rstrip("/")
    if relative == "":
        return ""
    if "\\" in relative:
        raise AppException(ErrorCode.BAD_REQUEST, f"非法路径：{path!r}", status_code=422)
    parts = PurePosixPath(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise AppException(ErrorCode.BAD_REQUEST, f"非法路径：{path!r}", status_code=422)
    return relative


class StagingBackend(BackendProtocol):
    """把 deepagents 文件工具映射到当前 run：读授权源码（叠加暂存），写专属暂存。"""

    def __init__(self, backend: FileBackend) -> None:
        self._fb = backend
        self._cache: dict[str, FileData] | None = None

    # ---- 视图构建 ----
    def _invalidate(self) -> None:
        self._cache = None

    def _collect_source(self, root: Path, files: dict[str, FileData]) -> None:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if relative == STAGING_DIRNAME or relative.startswith(STAGING_DIRNAME + "/"):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            files["/" + relative] = create_file_data(content)

    def _snapshot(self) -> dict[str, FileData]:
        if self._cache is not None:
            return self._cache
        files: dict[str, FileData] = {}
        self._collect_source(self._fb.run_dir, files)
        staging = self._fb.staging
        if staging.is_dir():
            for path in staging.rglob("*"):
                if not path.is_file():
                    continue
                relative = path.relative_to(staging).as_posix()
                if relative.endswith(".delete"):
                    files.pop("/" + relative[: -len(".delete")], None)
                    continue
                try:
                    content = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                files["/" + relative] = create_file_data(content)
        self._cache = files
        return files

    # ---- 读 ----
    def ls(self, path: str) -> LsResult:
        base = _relative_path(path)
        needle = "/" + base + "/" if base else "/"
        seen: dict[str, bool] = {}
        for vpath in self._snapshot():
            if not vpath.startswith(needle):
                continue
            rest = vpath[len(needle):]
            if not rest:
                continue
            head, sep, _tail = rest.partition("/")
            seen[head] = seen.get(head, False) or bool(sep)
        prefix = ("/" + base) if base else ""
        entries: list[FileInfo] = [
            {"path": f"{prefix}/{name}", "is_dir": is_dir}
            for name, is_dir in sorted(seen.items())
        ]
        return LsResult(entries=entries)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        key = "/" + _relative_path(file_path)
        file_data = self._snapshot().get(key)
        if file_data is None:
            return ReadResult(error=f"File not found: {file_path}")
        return slice_read_response(file_data, offset, limit)

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        base = _relative_path(path) if path else ""
        needle = "/" + base + "/" if base else "/"
        try:
            matcher = compile_grep_include_glob(pattern)
        except InvalidGlobPatternError as exc:
            return GlobResult(error=str(exc))
        matches: list[FileInfo] = []
        for vpath, file_data in self._snapshot().items():
            if not vpath.startswith(needle):
                continue
            relative = vpath[len(needle):]
            if matcher(relative):
                matches.append({"path": vpath, "modified_at": file_data.get("modified_at", "")})
        matches.sort(key=lambda item: item.get("modified_at", ""), reverse=True)
        return GlobResult(matches=matches)

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        base = ("/" + _relative_path(path)) if path else "/"
        return grep_matches_from_files(self._snapshot(), pattern, base, glob, max_count=max_count)

    # ---- 写（经 R15 FileBackend：allowed_paths / 受保护 / 暂存） ----
    def write(self, file_path: str, content: str) -> WriteResult:
        key = "/" + _relative_path(file_path)
        try:
            self._fb.write(_relative_path(file_path), content.encode("utf-8"))
        except AppException as exc:
            return WriteResult(error=exc.message)
        self._invalidate()
        return WriteResult(path=key)

    def edit(
        self, file_path: str, old_string: str, new_string: str, replace_all: bool = False
    ) -> EditResult:
        key = "/" + _relative_path(file_path)
        file_data = self._snapshot().get(key)
        if file_data is None:
            return EditResult(error=f"File not found: {file_path}")
        outcome = perform_string_replacement(file_data["content"], old_string, new_string, replace_all)
        if isinstance(outcome, str):
            return EditResult(error=outcome)
        new_content, occurrences = outcome
        try:
            self._fb.write(_relative_path(file_path), new_content.encode("utf-8"))
        except AppException as exc:
            return EditResult(error=exc.message)
        self._invalidate()
        return EditResult(path=key, occurrences=occurrences)

    def delete(self, file_path: str) -> DeleteResult:
        key = "/" + _relative_path(file_path)
        try:
            self._fb.delete(_relative_path(file_path))
        except AppException as exc:
            return DeleteResult(error=exc.message)
        self._invalidate()
        return DeleteResult(path=key)


# --------------------------------------------------------------------------- #
# 内部调用入账：包装 ChatOpenAI，逐次请求继承 run/task/call_id
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LedgerBinding:
    """把 Agent 内部每次模型请求绑定到既有账本（run/task 上下文）。"""

    session_factory: sessionmaker[Session]
    project_id: int
    run_id: int
    task_id: str
    model_role: str = "coding"
    max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS


def _usage_from_chat_result(result: Any) -> Usage | None:
    generations = getattr(result, "generations", None) or []
    if generations:
        message = getattr(generations[0], "message", None)
        usage_metadata = getattr(message, "usage_metadata", None)
        if usage_metadata:
            return Usage(
                int(usage_metadata.get("input_tokens", 0)),
                int(usage_metadata.get("output_tokens", 0)),
            )
    token_usage = (getattr(result, "llm_output", None) or {}).get("token_usage") or {}
    if {"prompt_tokens", "completion_tokens"} <= token_usage.keys():
        return Usage(int(token_usage["prompt_tokens"]), int(token_usage["completion_tokens"]))
    return None


class LedgerChatModel(ChatOpenAI):
    """每次 ``_generate``/``_agenerate`` 都先预留、后结算的 ChatOpenAI 包装。

    预留/结算共享 :mod:`agentforge_platform.model_gateway` 的预算护栏与幂等账本；
    预算不足时在**发起供应商请求之前**抛出，避免越额调用。
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    _binding: LedgerBinding = PrivateAttr()

    def __init__(self, *, ledger_binding: LedgerBinding, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._binding = ledger_binding

    def _reserve(self, call_id: str) -> None:
        binding = self._binding
        with session_scope(binding.session_factory) as db:
            model_gateway.reserve(
                db,
                call_id=call_id,
                model_role=binding.model_role,
                project_id=binding.project_id,
                max_input_tokens=binding.max_input_tokens,
                max_output_tokens=binding.max_output_tokens,
                run_id=binding.run_id,
                task_id=binding.task_id,
            )

    def _settle(self, call_id: str, usage: Usage | None, error_class: str | None = None) -> None:
        binding = self._binding
        with session_scope(binding.session_factory) as db:
            model_gateway.settle(db, call_id, usage=usage, error_class=error_class)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        call_id = uuid.uuid4().hex
        self._reserve(call_id)
        try:
            result = super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:  # 失败保留 UNKNOWN 预留供对账
            self._settle(call_id, usage=None, error_class=type(exc).__name__)
            raise
        self._settle(call_id, usage=_usage_from_chat_result(result))
        return result

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        call_id = uuid.uuid4().hex
        self._reserve(call_id)
        try:
            result = await super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
        except Exception as exc:  # 失败保留 UNKNOWN 预留供对账
            self._settle(call_id, usage=None, error_class=type(exc).__name__)
            raise
        self._settle(call_id, usage=_usage_from_chat_result(result))
        return result


def build_coding_model(
    *, ledger_binding: LedgerBinding, model_name: str | None = None
) -> LedgerChatModel:
    """构造 coding 角色的账本模型（百炼 OpenAI 兼容端点）；缺密钥时在调用前即拒绝。"""
    api_key = os.environ.get(CODING_API_KEY_ENV)
    if not api_key:
        raise ProviderPreflightError(f"环境变量 {CODING_API_KEY_ENV} 未配置")
    return LedgerChatModel(
        ledger_binding=ledger_binding,
        model=model_name or os.environ.get("AGENTFORGE_CODING_MODEL", DEFAULT_CODING_MODEL),
        base_url=dashscope_base_url(),
        api_key=api_key,
        max_tokens=ledger_binding.max_output_tokens,
        temperature=0,
    )


# --------------------------------------------------------------------------- #
# 工具表审计与引擎构造
# --------------------------------------------------------------------------- #
def _harness_key(model: BaseChatModel) -> str:
    """复现 deepagents 的 profile 键推导（预构造模型：``provider:identifier``）。"""
    from deepagents._models import get_model_identifier, get_model_provider

    provider = get_model_provider(model)
    identifier = get_model_identifier(model)
    if provider and identifier:
        return f"{provider}:{identifier}"
    return "openai"


def register_restricted_harness(model: BaseChatModel) -> str:
    """注册禁用默认 general-purpose 子代理、移除 ``execute`` 的 harness profile（幂等）。"""
    key = _harness_key(model)
    register_harness_profile(
        key,
        HarnessProfileConfig(
            excluded_tools={"execute"},
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        ),
    )
    return key


def audit_tool_table(agent: Any) -> set[str]:
    """读取已编译 agent 的**真实**工具表（``ToolNode.tools_by_name``）。"""
    node = getattr(agent, "nodes", {}).get("tools")
    bound = getattr(node, "bound", None)
    tools_by_name = getattr(bound, "tools_by_name", None)
    if isinstance(tools_by_name, dict):
        return set(tools_by_name)
    return set()


def assert_restricted_tools(agent: Any) -> set[str]:
    """构造后的第二道闸：出现 shell/委派/白名单之外的任何工具即拒绝。"""
    names = audit_tool_table(agent)
    forbidden = names & FORBIDDEN_TOOL_NAMES
    if forbidden:
        raise AppException(
            ErrorCode.FORBIDDEN, f"Agent 暴露危险工具：{sorted(forbidden)}", status_code=500
        )
    unexpected = names - set(RESTRICTED_TOOLS)
    if unexpected:
        raise AppException(
            ErrorCode.FORBIDDEN, f"Agent 暴露未授权工具：{sorted(unexpected)}", status_code=500
        )
    return names


@dataclass
class PatchEngine:
    """受限单任务 patch engine：一次 invoke 产出一份待提交变更集（不自动提交）。"""

    model: BaseChatModel
    backend: StagingBackend
    agent: Any
    tool_names: frozenset[str]

    def run(self, task_prompt: str, *, recursion_limit: int | None = None) -> dict[str, Any]:
        """一次受限 invoke。工具循环超过 R21 上限（每工具操作≈2 步）即中止并抛 ``TOOL_LIMIT_EXCEEDED``。"""
        limit = recursion_limit if recursion_limit is not None else runtime_limits().agent_recursion_limit
        try:
            result = self.agent.invoke(
                {"messages": [{"role": "user", "content": task_prompt}]},
                config={"recursion_limit": limit},
            )
        except GraphRecursionError as exc:
            raise AppException(
                ErrorCode.TOOL_LIMIT_EXCEEDED,
                f"Agent 工具循环超过上限（recursion_limit={limit}）",
                status_code=429,
            ) from exc
        return {
            "messages": result.get("messages", []),
            "change_set": self.backend._fb.build_change_set().to_dict(),
        }


def build_patch_engine(
    *,
    ledger_binding: LedgerBinding,
    file_backend: FileBackend,
    system_prompt: str,
    model_name: str | None = None,
    model: BaseChatModel | None = None,
    extra_tools: tuple[Callable[..., Any], ...] = (),
) -> PatchEngine:
    """用受限工具 + R15 暂存 backend 构造单任务 patch engine，并审计真实工具表。

    ``model`` 可注入（测试用脚本化模型）；默认按 coding 角色构造账本模型。
    """
    resolved = model or build_coding_model(ledger_binding=ledger_binding, model_name=model_name)
    register_restricted_harness(resolved)
    staging_backend = StagingBackend(file_backend)
    filesystem = FilesystemMiddleware(tools=list(RESTRICTED_TOOLS), backend=staging_backend)
    agent = create_deep_agent(
        model=resolved,
        system_prompt=system_prompt,
        tools=list(extra_tools),
        middleware=[filesystem],
        subagents=[],
    )
    tool_names = frozenset(assert_restricted_tools(agent))
    return PatchEngine(
        model=model, backend=staging_backend, agent=agent, tool_names=tool_names
    )


__all__ = [
    "CODING_API_KEY_ENV",
    "DEFAULT_CODING_MODEL",
    "DEFAULT_DASHSCOPE_BASE_URL",
    "FORBIDDEN_TOOL_NAMES",
    "RESTRICTED_TOOLS",
    "LedgerBinding",
    "LedgerChatModel",
    "PatchEngine",
    "StagingBackend",
    "assert_restricted_tools",
    "audit_tool_table",
    "build_coding_model",
    "build_patch_engine",
    "coding_enabled",
    "dashscope_base_url",
    "register_restricted_harness",
]
