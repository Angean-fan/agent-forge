"""F02：Module Memory Store —— 模块长期记忆、版本隔离与可信更新。

为每个业务模块提供**跨任务、跨新 run** 的专属版本化长期记忆，并保证记忆**不凌驾**于冻结规格、
接口版本或源码真值之上：

- **索引**：记忆以 ``(project_id, module_id, spec_hash/run 版本)`` 绑定；每条决策关联
  ``task_id/commit_hash/contract_revision`` 与**验证证据**；
- **可信更新**：只读加载；写入由受控服务依据**已提交且通过检查**的结果执行，**模型不得直接覆盖存储**；
- **隔离**：按 ``<root>/<project_id>/<module_id>.json`` 分文件，模块 A 无法读取 B 的私有记忆；
- **失效**：冻结 spec 或接口版本变化 → 冲突记忆标 ``STALE`` 并提示重建，**不**生成隐式业务规则；
- **回退**：按 ``revision`` 回滚记忆指针，避免悬空"已完成"；写入幂等（同键同值不双写）；
- **边界**：限制条目数与摘要体积；**不保存**秘密、原始对话或隐含推理（秘密扫描命中即拒）。

本模块**不引入向量库或云存储**；纯确定性、零模型调用。R23 的 checkpoint 解决**执行恢复**，
与此处**跨 run 的长期记忆**是两回事。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.external_requirements import scan_for_secrets

MEMORY_VERSION = "0.1"
STATUS_VERIFIED = "VERIFIED"
STATUS_STALE = "STALE"
READ_CURRENT = "CURRENT"
READ_REBUILD = "REBUILD_REQUIRED"

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_KEY = re.compile(r"[A-Za-z0-9._\-]{1,80}\Z")
_STRICT = ConfigDict(extra="forbid")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemoryEntry(BaseModel):
    """一条经**验证**的模块决策（脱敏事实，非原始对话/隐含推理）。"""

    model_config = _STRICT

    key: str
    value: str
    spec_hash: str
    contract_revision: int
    task_id: str | None = None
    commit_hash: str | None = None
    verification_ref: str | None = None
    status: str = STATUS_VERIFIED
    revision: int = 0
    created_at: str = ""

    @field_validator("key")
    @classmethod
    def _safe_key(cls, value: str) -> str:
        if _SAFE_KEY.fullmatch(value) is None:
            raise ValueError(f"非法记忆键：{value!r}")
        return value


class ModuleMemory(BaseModel):
    model_config = _STRICT

    schema_version: str = MEMORY_VERSION
    project_id: str
    module_id: str
    spec_hash: str
    memory_revision: int = 0
    contract_revision: int = 0
    entries: list[MemoryEntry] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryView:
    """只读读取结果：仅含与当前 spec/接口**兼容**的条目。"""

    module_id: str
    status: str
    memory_revision: int
    entries: tuple[MemoryEntry, ...]
    notice: str | None = None


def _reject(message: str, *, status_code: int = 422) -> AppException:
    return AppException(ErrorCode.MODULE_MEMORY_REJECTED, message, status_code=status_code)


class ModuleMemoryStore:
    """文件级（每模块一文件）的模块长期记忆存储；只读加载、受控写入、隔离与回退。"""

    def __init__(self, root: str | Path, *, max_entries: int = 200, max_value_chars: int = 2000):
        self.root = Path(root)
        self.max_entries = max_entries
        self.max_value_chars = max_value_chars

    def path_for(self, project_id: str, module_id: str) -> Path:
        # 路径段做安全校验（无穿越/绝对路径），保证模块间物理隔离。
        for segment in (project_id, module_id):
            if not _SAFE_KEY.fullmatch(segment):
                raise _reject(f"非法 project/module id：{segment!r}")
        return self.root / project_id / f"{module_id}.json"

    # ---- 读（只读；越权/缺失返回 None，不跨模块泄漏） ----
    def load(self, project_id: str, module_id: str) -> ModuleMemory | None:
        path = self.path_for(project_id, module_id)
        if not path.is_file():
            return None
        return ModuleMemory.model_validate_json(path.read_text(encoding="utf-8"))

    def read(
        self,
        project_id: str,
        module_id: str,
        *,
        spec_hash: str,
        contract_revision: int | None = None,
    ) -> MemoryView:
        """按当前冻结 spec/接口读取**兼容**记忆；不兼容 → ``REBUILD_REQUIRED`` 且仅返回兼容条目。"""
        memory = self.load(project_id, module_id)
        if memory is None:
            return MemoryView(module_id, READ_CURRENT, 0, ())
        compatible = [
            entry for entry in memory.entries
            if entry.spec_hash == spec_hash
            and (contract_revision is None or entry.contract_revision == contract_revision)
            and entry.status == STATUS_VERIFIED
        ]
        stale = len(compatible) != len(memory.entries)
        status = READ_REBUILD if stale else READ_CURRENT
        notice = (
            "冻结规格或接口版本已变化：冲突记忆失效，需按新版本重建。" if stale else None
        )
        return MemoryView(module_id, status, memory.memory_revision, tuple(compatible), notice)

    # ---- 写（受控；仅"已提交且通过检查"的结果） ----
    def record(
        self,
        project_id: str,
        module_id: str,
        *,
        key: str,
        value: str,
        spec_hash: str,
        contract_revision: int,
        verified: bool,
        committed: bool,
        task_id: str | None = None,
        commit_hash: str | None = None,
        verification_ref: str | None = None,
    ) -> ModuleMemory:
        """记录一条经验证的决策；未验证/未提交、含秘密、超限一律拒绝。幂等：同键同值不双写。"""
        if not (verified and committed):
            raise _reject("拒绝写入未经验证或未提交的模块记忆。")
        if len(value) > self.max_value_chars:
            raise _reject(f"记忆值超限（>{self.max_value_chars} 字符）。")
        findings = scan_for_secrets(value)
        if findings:
            raise _reject("记忆含疑似秘密，拒绝写入：" + "；".join(findings))
        if commit_hash is not None and _HEX64.fullmatch(commit_hash) is None:
            raise _reject(f"非法 commit_hash：{commit_hash!r}")

        memory = self.load(project_id, module_id) or ModuleMemory(
            project_id=project_id, module_id=module_id, spec_hash=spec_hash,
            contract_revision=contract_revision,
        )
        existing = next((e for e in memory.entries if e.key == key), None)
        if (
            existing is not None
            and existing.value == value
            and existing.spec_hash == spec_hash
            and existing.commit_hash == commit_hash
        ):
            return memory  # 幂等：同键同值同证据 → 不双写、不递增修订

        if existing is None and len(memory.entries) >= self.max_entries:
            raise _reject(f"模块记忆条目超限（>{self.max_entries}）。")

        revision = memory.memory_revision + 1
        entry = MemoryEntry(
            key=key, value=value, spec_hash=spec_hash, contract_revision=contract_revision,
            task_id=task_id, commit_hash=commit_hash, verification_ref=verification_ref,
            status=STATUS_VERIFIED, revision=revision, created_at=_now(),
        )
        entries = [e for e in memory.entries if e.key != key] + [entry]
        updated = memory.model_copy(update={
            "spec_hash": spec_hash, "contract_revision": contract_revision,
            "memory_revision": revision, "entries": entries,
        })
        self._persist(updated)
        return updated

    def rollback(self, project_id: str, module_id: str, *, to_revision: int) -> ModuleMemory:
        """记忆指针随代码版本回滚：丢弃晚于 ``to_revision`` 的条目，避免悬空"已完成"。"""
        memory = self.load(project_id, module_id)
        if memory is None:
            raise _reject(f"无模块记忆可回滚：{module_id}", status_code=404)
        if to_revision < 0 or to_revision > memory.memory_revision:
            raise _reject(f"非法回滚目标：{to_revision}")
        kept = [e for e in memory.entries if e.revision <= to_revision]
        updated = memory.model_copy(update={"entries": kept, "memory_revision": to_revision})
        self._persist(updated)
        return updated

    def _persist(self, memory: ModuleMemory) -> None:
        path = self.path_for(memory.project_id, memory.module_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(memory.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)  # 原子替换：崩溃不产生半写文件


__all__ = [
    "MEMORY_VERSION",
    "READ_CURRENT",
    "READ_REBUILD",
    "STATUS_STALE",
    "STATUS_VERIFIED",
    "MemoryEntry",
    "MemoryView",
    "ModuleMemory",
    "ModuleMemoryStore",
]
