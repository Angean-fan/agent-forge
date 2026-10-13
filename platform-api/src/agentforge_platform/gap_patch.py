"""R17：装配优先，缺口局部 Patch（`gap_check → (reuse | patch) → commit`）。

对 R14 产生的**真实缺口**逐条决策：

1. **装配优先（reuse）**：若 Finder 已筛出**兼容且声明该验收**的方法单元（``search.selected_refs`` 命中方法仓库），
   按装配器的方法单元 COPY 语义**增量装入** run 树（不整体重建、不覆盖既有文件），**不调用 Coding Agent**；
2. **局部 Patch（patch）**：无兼容方法、缺口在项目扩展点内（有 ``allowed_paths``）时，交给 R16 受限引擎产出变更集，
   经**确定性校验**后按 R15 ``FileBackend`` 原子提交；
3. **拒绝（refuse）**：缺口未给出可安全工作的扩展点（``allowed_paths`` 为空）→ 保持缺口，不伪装覆盖。

约束：Agent 只消费**已确认 spec / 冻结 contract / ``allowed_paths``**（不接触 DeepSeek 对话历史、不扩大需求、不改模板原件、
不升级依赖、不改固定测试）；**模型输出不自动登记为新 Method Unit**；每次 Patch 与**检索轨迹**（query_hash/候选/拒绝原因）互相关联。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agentforge_platform.agent_files import FileBackend
from agentforge_platform.asset_finder import AssetSearchResult
from agentforge_platform.asset_registry import MethodUnitManifest
from agentforge_platform.contract import BusinessContract
from agentforge_platform.coverage import CoverageGap
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.method_index import MethodRecord, scan_methods
from agentforge_platform.resolver import AssetLock
from agentforge_platform.spec import SoftwareSpecification

REUSE = "reuse"
PATCH = "patch"
REFUSE = "refuse"

_MAX_CHANGES = 32


def _record_index(methods_root: str | Path) -> dict[tuple[str, str], MethodRecord]:
    return {(record.id, record.version): record for record in scan_methods(methods_root)}


def _split_ref(ref: str) -> tuple[str, str, str]:
    """``method-unit:status-guard@1.0.0`` → ``("method-unit", "status-guard", "1.0.0")``。"""
    kind, _, rest = ref.partition(":")
    asset_id, _, version = rest.partition("@")
    return kind, asset_id, version


def reuse_ref(gap: CoverageGap, search: AssetSearchResult, methods_root: str | Path) -> str | None:
    """装配优先：Finder 已选中的**兼容**方法单元里，是否有**声明该验收**的可复用资产。"""
    records = _record_index(methods_root)
    for ref in search.selected_refs:
        kind, asset_id, version = _split_ref(ref)
        if kind != "method-unit":
            continue
        record = records.get((asset_id, version))
        if record is not None and gap.acceptance_id in record.acceptance_ids:
            return ref
    return None


@dataclass(frozen=True)
class GapDecision:
    gap: CoverageGap
    action: str
    reuse_ref: str | None
    reason: str


def choose_gap_action(
    gap: CoverageGap, search: AssetSearchResult, methods_root: str | Path
) -> GapDecision:
    ref = reuse_ref(gap, search, methods_root)
    if ref is not None:
        return GapDecision(gap, REUSE, ref, "存在经验证且声明该验收的兼容方法单元，优先复用装配")
    if not gap.allowed_paths:
        return GapDecision(gap, REFUSE, None, "缺口未给出可安全工作的扩展点，保持缺口")
    return GapDecision(gap, PATCH, None, gap.reason)


# --------------------------------------------------------------------------- #
# TaskContext（冻结上下文；不含对话历史）
# --------------------------------------------------------------------------- #
def build_task_context(
    gap: CoverageGap,
    spec: SoftwareSpecification,
    contract: BusinessContract,
    lock: AssetLock,
    *,
    run_dir: str | Path,
) -> dict:
    """构造 Agent 的**冻结**上下文：规格片段 + 契约 + 授权文件 + 依赖，**不含**原始对话。"""
    root = Path(run_dir)
    acceptance = [
        case.model_dump(mode="json") for case in spec.acceptance_cases if case.id == gap.acceptance_id
    ]
    authorized: list[str] = []
    for allowed in gap.allowed_paths:
        base = root / allowed
        if base.is_dir():
            authorized += sorted(
                path.relative_to(root).as_posix() for path in base.rglob("*") if path.is_file()
            )
    return {
        "task_id": gap.task_id,
        "acceptance_id": gap.acceptance_id,
        "gap_code": gap.code,
        "gap_reason": gap.reason,
        "allowed_paths": list(gap.allowed_paths),
        "spec_fragment": {
            "goal": spec.goal,
            "entities": [entity.model_dump(mode="json") for entity in spec.entities],
            "acceptance": acceptance,
        },
        "contract": {
            "routes": [route.model_dump(mode="json") for route in contract.routes],
            "tables": [table.model_dump(mode="json") for table in contract.tables],
        },
        "locked_assets": [
            f"{asset.kind}:{asset.id}@{asset.version}" for asset in lock.assets
        ] + [f"method-unit:{unit.id}@{unit.version}" for unit in lock.method_units],
        "authorized_files": authorized,
        "prohibited": [
            "扩大或改写需求", "修改模板原件/公共方法", "自动升级依赖", "重写整个模块", "修改固定测试",
        ],
    }


def render_task_prompt(context: dict) -> str:
    """把冻结上下文渲染为确定性提示（稳定、可复现）。"""
    return (
        "你是受限补缺编码器。仅依据下列**已冻结**上下文做**最小**修改，只写 allowed_paths 内文件，"
        "不得扩大需求、修改模板原件或固定测试、升级依赖或重写模块。完成后停止。\n"
        + json.dumps(context, ensure_ascii=False, indent=2, sort_keys=True)
    )


# --------------------------------------------------------------------------- #
# 变更集校验与提交
# --------------------------------------------------------------------------- #
def _within_allowed(relative: str, allowed: list[str]) -> bool:
    for root in allowed:
        root = root.rstrip("/")
        if root in ("", "."):
            return True
        if relative == root or relative.startswith(root + "/"):
            return True
    return False


def validate_change_set(change_set: dict, *, allowed_paths: list[str]) -> None:
    """提交前的确定性校验：路径安全、在 allowed_paths 内、动作/数量合法。非法即拒绝（不提交）。"""
    changes = change_set.get("changes", [])
    if not isinstance(changes, list) or not changes:
        raise AppException(ErrorCode.VALIDATION_ERROR, "变更集为空", status_code=422)
    if len(changes) > _MAX_CHANGES:
        raise AppException(
            ErrorCode.VALIDATION_ERROR, f"变更文件数超上限（{len(changes)}）", status_code=422
        )
    for change in changes:
        path = change.get("path", "")
        if change.get("action") not in {"create", "modify", "delete"}:
            raise AppException(ErrorCode.VALIDATION_ERROR, f"非法动作：{change.get('action')}", 422)
        if not path or path.startswith(("/", "~")) or "\\" in path:
            raise AppException(ErrorCode.VALIDATION_ERROR, f"非法路径：{path!r}", status_code=422)
        parts = PurePosixPath(path).parts
        if any(part in ("", ".", "..") for part in parts):
            raise AppException(ErrorCode.VALIDATION_ERROR, f"非法路径：{path!r}", status_code=422)
        if not _within_allowed(path, allowed_paths):
            raise AppException(
                ErrorCode.VALIDATION_ERROR, f"路径不在 allowed_paths 内：{path}", status_code=422
            )


def _correlation(search: AssetSearchResult) -> dict:
    return {
        "query_hash": search.query_hash,
        "index_revision": search.index_revision,
        "match_type": search.match_type,
        "candidate_refs": list(search.candidate_refs),
        "selected_refs": list(search.selected_refs),
        "filter_reasons": {ref: list(reasons) for ref, reasons in search.filter_reasons.items()},
    }


def apply_gap_patch(
    engine,  # PatchEngine（R16）
    file_backend: FileBackend,
    gap: CoverageGap,
    context: dict,
    *,
    search: AssetSearchResult,
) -> dict:
    """调用受限引擎产出变更集 → 校验 → 原子提交；返回可审查结果与检索轨迹关联。"""
    result = engine.run(render_task_prompt(context))
    staged_change_set = result.get("change_set", {})
    validate_change_set(staged_change_set, allowed_paths=list(gap.allowed_paths))
    committed = file_backend.commit()
    return {
        "task_id": gap.task_id,
        "acceptance_id": gap.acceptance_id,
        "action": PATCH,
        "change_set": committed.to_dict(),
        "correlation": _correlation(search),
    }


# --------------------------------------------------------------------------- #
# 装配优先：把兼容方法单元增量装入 run 树（按装配器 COPY 语义）
# --------------------------------------------------------------------------- #
def _safe_target(run_dir: Path, relative: str) -> Path:
    if not relative or relative.startswith("/") or "\\" in relative:
        raise AppException(ErrorCode.ASSEMBLY_ERROR, f"非法路径：{relative!r}", status_code=422)
    parts = PurePosixPath(relative).parts
    if any(part in ("", ".", "..") for part in parts):
        raise AppException(ErrorCode.ASSEMBLY_ERROR, f"非法路径：{relative!r}", status_code=422)
    return run_dir.joinpath(*parts)


def assemble_reuse_method(
    run_dir: str | Path, record: MethodRecord, methods_root: str | Path
) -> list[str]:
    """按装配器语义把方法单元 ``src/`` COPY 进其 ``target_dir``（不覆盖既有文件）。"""
    root = Path(run_dir)
    directory = Path(methods_root) / record.id / record.version
    manifest = MethodUnitManifest.model_validate_json(
        (directory / "manifest.json").read_text(encoding="utf-8")
    )
    target = (manifest.target_dir or (manifest.owned_paths[0] if manifest.owned_paths else "")).rstrip("/")
    if not target:
        raise AppException(ErrorCode.ASSEMBLY_ERROR, f"方法单元未声明安装目标：{record.id}", 422)
    written: list[str] = []
    source = directory / "src"
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise AppException(ErrorCode.ASSEMBLY_ERROR, f"方法单元含符号链接：{path}", 422)
        if not path.is_file():
            continue
        relative = path.relative_to(source).as_posix()
        destination = _safe_target(root, f"{target}/{relative}")
        if destination.exists():
            raise AppException(
                ErrorCode.ASSEMBLY_ERROR, f"复用装配将覆盖既有文件：{target}/{relative}", 422
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(path.read_bytes())
        written.append(f"{target}/{relative}")
    return written


@dataclass
class GapResolution:
    task_id: str
    acceptance_id: str
    action: str
    reason: str
    detail: dict


def resolve_gaps(
    gaps: tuple[CoverageGap, ...] | list[CoverageGap],
    *,
    search: AssetSearchResult,
    spec: SoftwareSpecification,
    contract: BusinessContract,
    lock: AssetLock,
    run_dir: str | Path,
    methods_root: str | Path,
    engine_factory=None,  # Callable[[list[str], str], tuple[PatchEngine, FileBackend]] | None
) -> list[GapResolution]:
    """逐缺口执行：reuse（增量装配，不调用 Agent）/ patch（受限引擎）/ refuse（保持缺口）。

    ``engine_factory(allowed_paths, task_id)`` 按缺口返回绑定好账本上下文的 ``(PatchEngine, FileBackend)``；
    为 ``None`` 时缺口保持 ``refuse``，不伪装覆盖。
    """
    resolutions: list[GapResolution] = []
    for gap in gaps:
        decision = choose_gap_action(gap, search, methods_root)
        if decision.action == REUSE:
            records = _record_index(methods_root)
            _kind, asset_id, version = _split_ref(decision.reuse_ref or "")
            record = records[(asset_id, version)]
            written = assemble_reuse_method(run_dir, record, methods_root)
            resolutions.append(
                GapResolution(gap.task_id, gap.acceptance_id, REUSE, decision.reason, {
                    "reused": decision.reuse_ref,
                    "written": written,
                    # 复用即“已装配资产”：供覆盖率重算把该验收计入 covered。
                    "locked_unit": {
                        "kind": "method-unit", "id": record.id, "version": record.version,
                        "checksum": record.checksum, "path": record.asset_path,
                    },
                })
            )
        elif decision.action == PATCH:
            built = engine_factory(list(gap.allowed_paths), gap.task_id) if engine_factory else None
            if built is None:
                resolutions.append(
                    GapResolution(gap.task_id, gap.acceptance_id, REFUSE,
                                  "未配置受限引擎，暂不 Patch", {})
                )
                continue
            engine, file_backend = built
            context = build_task_context(gap, spec, contract, lock, run_dir=run_dir)
            detail = apply_gap_patch(engine, file_backend, gap, context, search=search)
            resolutions.append(
                GapResolution(gap.task_id, gap.acceptance_id, PATCH, decision.reason, detail)
            )
        else:
            resolutions.append(
                GapResolution(gap.task_id, gap.acceptance_id, REFUSE, decision.reason, {})
            )
    return resolutions


__all__ = [
    "PATCH",
    "REFUSE",
    "REUSE",
    "GapDecision",
    "GapResolution",
    "apply_gap_patch",
    "assemble_reuse_method",
    "build_task_context",
    "choose_gap_action",
    "render_task_prompt",
    "resolve_gaps",
    "reuse_ref",
    "validate_change_set",
]
