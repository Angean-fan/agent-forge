"""F08：独立模块并行调度（**可选、默认禁用、需人工批准**）。

仅在串行模块 Agent 通过 F07 后，探索**无依赖**模块的有限并行，同时保证资产锁、模块代码/接口发布、
共享预算与恢复的一致性。

硬性边界（对齐《AgentForge_03》F08）：

- **双重开关**：``AGENTFORGE_MODULE_PARALLEL_ENABLED`` **且** 人工批准 ``AGENTFORGE_MODULE_PARALLEL_APPROVED``
  才生效；缺任一 → 回退 **F07 已通过的串行模式**（本模块不改串行语义）。
- **不以"路径看起来不重叠"作为安全证明**：并行波次在 **依赖 DAG** 之外，还要排除 **migration 命名空间**、
  **DTO 命名**等共享资源冲突；冲突模块**自动串行**（后置波次）或阻塞。
- **每模块暂存隔离**（:class:`ModuleStaging`）+ **合并冲突检测**（:meth:`ModuleStaging.merge_issues`）。
- **并发预算预留**（:class:`BudgetPool`，加锁）→ **不双扣**；**任务幂等**（同模块重复执行只取一次）。

纯确定性；真实多 worker/并发 DB 的扩容评估不在本模块（需另立基础设施与预算）。
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FLAG_ENV = "AGENTFORGE_MODULE_PARALLEL_ENABLED"
APPROVAL_ENV = "AGENTFORGE_MODULE_PARALLEL_APPROVED"
MIGRATION_RESOURCE = "migration"


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


def parallel_enabled() -> bool:
    """并行调度是否生效：**开关 + 人工批准**双条件（默认关闭）。"""
    return _env_true(FLAG_ENV) and _env_true(APPROVAL_ENV)


@dataclass(frozen=True)
class ParallelPlan:
    """拓扑分波：同一波内模块**数据/资源不相交**，可有限并行。"""

    waves: tuple[tuple[str, ...], ...]
    serialized_reasons: tuple[str, ...] = ()

    @property
    def parallel(self) -> bool:
        return any(len(wave) > 1 for wave in self.waves)

    def serial_order(self) -> tuple[str, ...]:
        return tuple(mid for wave in self.waves for mid in wave)


def _resources(module: Any) -> dict[str, str]:
    """模块占用的**共享资源**标签（migration 命名空间 / DTO 命名 / 具体文件）。"""
    tags: dict[str, str] = {}
    for path in module.owned_paths:
        if "/db/migration/" in path:
            tags.setdefault(MIGRATION_RESOURCE, path)  # 全局版本命名空间：互斥
        base = path.rsplit("/", 1)[-1]
        if "/dto/" in path:
            tags.setdefault(f"dto:{base}", path)
        tags.setdefault(f"file:{path}", path)
    return tags


def _conflicts(left: Any, right: Any) -> list[str]:
    shared = set(_resources(left)) & set(_resources(right))
    return sorted(shared)


def _topological(modules: list[Any]) -> list[Any]:
    by_id = {m.module_id: m for m in modules}
    indeg = {mid: 0 for mid in by_id}
    for module in modules:
        for dep in module.depends_on_modules:
            if dep in indeg:
                indeg[module.module_id] += 1
    ready = sorted(mid for mid, deg in indeg.items() if deg == 0)
    order: list[Any] = []
    while ready:
        mid = ready.pop(0)
        order.append(by_id[mid])
        for other in modules:
            if mid in other.depends_on_modules:
                indeg[other.module_id] -= 1
                if indeg[other.module_id] == 0:
                    ready.append(other.module_id)
                    ready.sort()
    return order  # 有环时仅返回已剥离的部分（由调用方/上游校验阻塞）


def plan_waves(module_registry: Any) -> ParallelPlan:
    """按依赖 DAG 拓扑分层；同层内**排除共享资源冲突**后分波（冲突模块自动串行）。"""
    modules = list(module_registry.modules)
    by_id = {m.module_id: m for m in modules}
    order = _topological(modules)
    level: dict[str, int] = {}
    for module in order:
        deps = [d for d in module.depends_on_modules if d in level]
        level[module.module_id] = max((level[d] + 1 for d in deps), default=0)

    waves: list[list[str]] = []
    reasons: list[str] = []
    for module in order:
        start = level[module.module_id]
        while len(waves) <= start:
            waves.append([])
        placed = False
        for index in range(start, len(waves)):
            if any(_conflicts(module, by_id[mid]) for mid in waves[index]):
                continue
            waves[index].append(module.module_id)
            placed = True
            break
        if not placed:
            if any(_conflicts(module, by_id[mid]) for mid in waves[start]):
                reasons.append(f"{module.module_id} 与同波模块存在共享资源冲突 → 后置串行")
            waves.append([module.module_id])
    return ParallelPlan(waves=tuple(tuple(w) for w in waves), serialized_reasons=tuple(reasons))


class BudgetPool:
    """并发预算：加锁预留/结算/释放，**不双扣**。"""

    def __init__(self, total_micro: int) -> None:
        self._total = total_micro
        self._reserved = 0
        self._settled = 0
        self._lock = threading.Lock()

    def reserve(self, amount_micro: int) -> bool:
        with self._lock:
            if self._reserved + self._settled + amount_micro > self._total:
                return False
            self._reserved += amount_micro
            return True

    def release(self, amount_micro: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - amount_micro)

    def settle(self, *, reserved_micro: int, actual_micro: int) -> None:
        with self._lock:
            self._reserved = max(0, self._reserved - reserved_micro)
            self._settled += actual_micro

    @property
    def state(self) -> dict[str, int]:
        with self._lock:
            return {"total": self._total, "reserved": self._reserved,
                    "settled": self._settled,
                    "available": self._total - self._reserved - self._settled}


class ModuleStaging:
    """每模块**独立暂存目录** + 合并冲突检测。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._staged: dict[str, dict[str, bytes]] = {}

    def stage(self, module_id: str, files: dict[str, bytes]) -> Path:
        directory = self.root / module_id
        directory.mkdir(parents=True, exist_ok=True)
        for relative, data in files.items():
            target = directory / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        self._staged[module_id] = dict(files)
        return directory

    def merge_issues(self) -> list[str]:
        """最终合并冲突检测：不同模块暂存**同一路径且内容不同** → 冲突（禁止覆盖）。"""
        owners: dict[str, tuple[str, str]] = {}
        issues: list[str] = []
        for module_id, files in sorted(self._staged.items()):
            for relative, data in sorted(files.items()):
                digest = hashlib.sha256(data).hexdigest()
                if relative in owners:
                    other_module, other_digest = owners[relative]
                    if other_module != module_id and other_digest != digest:
                        issues.append(f"合并冲突：{relative} 被 {other_module} 与 {module_id} 同时写入")
                else:
                    owners[relative] = (module_id, digest)
        return issues


def run_wave(
    module_ids: Iterable[str],
    runner: Callable[[str], dict],
    *,
    budget: BudgetPool | None = None,
    reserve_micro: int = 0,
    max_workers: int | None = None,
    done: set[str] | None = None,
) -> dict[str, Any]:
    """在同一波内**有限并行**执行模块任务；预算不足者跳过（不双扣）；**幂等**（已 done 的不重复）。

    ``runner(module_id) -> {"change_set": [...], "cost_micro": int}``。
    """
    completed_module_ids: set[str] = set(done or ())
    results: dict[str, Any] = {}
    lock = threading.Lock()

    def _task(module_id: str) -> None:
        if module_id in completed_module_ids:
            with lock:
                results[module_id] = {"status": "SKIPPED_IDEMPOTENT"}
            return
        if budget is not None and not budget.reserve(reserve_micro):
            with lock:
                results[module_id] = {"status": "BUDGET_BLOCKED"}
            return
        try:
            outcome = runner(module_id)
        except Exception as exc:  # noqa: BLE001 — 单模块失败不拖垮同波其他模块
            if budget is not None:
                budget.release(reserve_micro)
            with lock:
                results[module_id] = {"status": "FAILED", "error": type(exc).__name__}
            return
        if budget is not None:
            budget.settle(reserved_micro=reserve_micro,
                          actual_micro=int(outcome.get("cost_micro", reserve_micro)))
        with lock:
            results[module_id] = {"status": "DONE", **outcome}
            completed_module_ids.add(module_id)

    workers = max_workers or max(1, len(list(module_ids)))
    module_list = list(module_ids)
    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(module_list)))) as pool:
        list(pool.map(_task, module_list))
    return results


__all__ = [
    "APPROVAL_ENV",
    "FLAG_ENV",
    "MIGRATION_RESOURCE",
    "BudgetPool",
    "ModuleStaging",
    "ParallelPlan",
    "parallel_enabled",
    "plan_waves",
    "run_wave",
]
