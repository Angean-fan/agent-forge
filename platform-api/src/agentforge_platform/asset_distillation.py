"""E10：从已验收个案提炼**公共方法资产**并发布索引新版本（技术文档 §2/§6；需求第 9 节）。

治理闭环（**人工审核、不自动入库**）：
``候选提名 → 来源/许可证/敏感信息审查 → 提取最小功能单元 → 声明契约 → 独立测试与跨 Profile 契约测试 →
人工批准 → 发布不可变 asset ID/version/checksum → 原子重建 FTS5 索引``。

- 只从**已通过项目完整 Runner 验收**且来源/许可证可审计的变更提炼；**已验收 ≠ 通用性已证实**，不得自动入库；
- 候选含秘密/项目私有标记、缺独立测试/跨 Profile 契约/通用性说明 → **拒绝**，留在审查区，**污染不了公共库**；
- **已发布版本不可原地修改**；发布用新 ``asset_id@version``；
- **运行中 Agent 无公共仓库写权限**——本模块是离线审核/发布工具，不暴露给 Agent
  （``assert_not_agent_writable``）；
- **不凭方法名/向量相似度提覆盖率**，首版不引入向量库（沿用 R06A—R06D 的 FTS5 索引）。
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from agentforge_platform.asset_registry import (
    REPO_ROOT,
    MethodUnitManifest,
    compute_checksum,
)
from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.method_index import build_index, is_valid_index, read_meta, scan_methods

# 项目私有包/路径标记：必须**去项目化**后才能入公共库。
DEFAULT_PRIVATE_MARKERS = ("com.agentforge.template",)
REVIEW_DIRNAME = ".review"

_SECRET_ASSIGN = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL)[A-Z0-9_]*)\s*[:=]\s*\S+"
)
_PEM = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")
_AWS = re.compile(r"\bAKIA[0-9A-Z]{16}\b")


@dataclass(frozen=True)
class Candidate:
    """一条待审核的资产候选（含来源与拟发布内容）。"""

    asset_id: str
    version: str
    origin_run_id: int
    origin_acceptance_passed: bool  # 来源项目已通过完整 Runner 验收
    provenance: str                 # 可审计来源（如 run/patch 引用）
    license: str
    generality: str                 # 通用性说明（为何可复用，而非项目私有）
    cross_profiles: tuple[str, ...]  # 跨 Profile 契约测试覆盖
    manifest: dict                  # MethodUnitManifest 字段（symbol/signature/layer/…）
    source_file: str
    source_text: str
    test_file: str
    test_text: str


@dataclass(frozen=True)
class CandidateReview:
    approved: bool
    issues: list[str] = field(default_factory=list)
    scrub_findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PublishedAsset:
    asset_ref: str
    version: str
    checksum: str
    index_revision: str
    path: str


@dataclass(frozen=True)
class ReuseMetrics:
    """复用收益度量（对比发布前后）。"""

    retrieval_hit_rate: float
    compatibility_pass_rate: float
    reuse_rate: float
    acceptance_success_rate: float
    model_calls: int
    cost_micro: int
    duration_ms: int


def sensitive_findings(
    text: str, *, private_markers: Iterable[str] = DEFAULT_PRIVATE_MARKERS
) -> list[str]:
    """敏感/私有信息审查：秘密赋值、PEM/JWT/AWS、项目私有标记。"""
    findings: list[str] = []
    if _SECRET_ASSIGN.search(text):
        findings.append("疑似秘密赋值（KEY/SECRET/TOKEN/PASSWORD/PRIVATE）")
    if _PEM.search(text):
        findings.append("包含 PEM 私钥块")
    if _JWT.search(text):
        findings.append("包含疑似 JWT")
    if _AWS.search(text):
        findings.append("包含疑似 AWS Access Key")
    for marker in private_markers:
        if marker and marker in text:
            findings.append(f"含项目私有标记，须去项目化：{marker}")
    return findings


def scrub_source(text: str) -> str:
    """抽离密钥：把秘密赋值/PEM/JWT/AWS 替换为占位（**不**自动去项目化标记，需人工处理）。"""
    scrubbed = _SECRET_ASSIGN.sub(lambda match: f"{match.group(1)}=***", text)
    scrubbed = _PEM.sub("-----BEGIN *** PRIVATE KEY-----", scrubbed)
    scrubbed = _JWT.sub("***", scrubbed)
    scrubbed = _AWS.sub("***", scrubbed)
    return scrubbed


def review_candidate(candidate: Candidate, *, known_profiles: set[str]) -> CandidateReview:
    """确定性审查门禁；任一不满足即**拒绝**（候选不自动入库，留在审查区）。"""
    issues: list[str] = []
    if not candidate.origin_acceptance_passed:
        issues.append("来源项目未通过完整 Runner 验收，不得提炼")
    if not candidate.provenance.strip():
        issues.append("缺少可审计来源（provenance）")
    if candidate.license.strip().lower() in {"", "unknown", "unlicensed", "none", "proprietary-unknown"}:
        issues.append("许可证/来源不明")
    if not candidate.generality.strip():
        issues.append("未声明通用性（不得自动入库）")
    if not candidate.test_text.strip():
        issues.append("缺少独立单元/契约测试")
    if not candidate.cross_profiles:
        issues.append("缺少跨 Profile 契约测试")
    elif known_profiles and not (set(candidate.cross_profiles) & known_profiles):
        issues.append("跨 Profile 契约测试未覆盖任何已知 Profile")

    scrub_findings = sensitive_findings(candidate.source_text) + sensitive_findings(candidate.test_text)
    if scrub_findings:
        issues.append("源码/测试含敏感或项目私有信息，须先抽离")

    return CandidateReview(approved=not issues, issues=issues, scrub_findings=scrub_findings)


def assert_not_agent_writable(*, caller: str) -> None:
    """运行中 Agent 无公共仓库写权限；发布只能由**离线、人工**调用方执行。"""
    if caller != "review_tool":
        raise AppException(
            ErrorCode.FORBIDDEN,
            "运行中 Agent 无公共方法库写权限；发布须经人工审核工具。",
            status_code=403,
        )


def _manifest_payload(candidate: Candidate, checksum: str) -> dict:
    payload = dict(candidate.manifest)
    payload.update({
        "id": candidate.asset_id,
        "version": candidate.version,
        "kind": "method-unit",
        "license": candidate.license,
        "source_provenance": candidate.provenance,
        "checksum": checksum,
    })
    return payload


def publish_candidate(
    candidate: Candidate,
    review: CandidateReview,
    methods_root: str | Path,
    *,
    index_path: str | Path,
    approved_by: str,
    caller: str = "review_tool",
) -> PublishedAsset:
    """发布为**不可变**公共方法资产并原子重建索引；未审核/无批准人/版本已存在则拒绝。"""
    assert_not_agent_writable(caller=caller)
    if not review.approved:
        raise AppException(
            ErrorCode.ASSET_CONFLICT, "候选未通过审核，拒绝发布：" + "；".join(review.issues), status_code=422
        )
    if not approved_by.strip():
        raise AppException(ErrorCode.ASSET_CONFLICT, "缺少人工批准人，拒绝发布。", status_code=422)

    root = Path(methods_root)
    target = root / candidate.asset_id / candidate.version
    if target.exists():
        raise AppException(
            ErrorCode.ASSET_CONFLICT, f"已发布版本不可原地修改：{candidate.asset_id}@{candidate.version}",
            status_code=409,
        )

    scrubbed_source = scrub_source(candidate.source_text)
    (target / "src").mkdir(parents=True, exist_ok=True)
    (target / "tests").mkdir(parents=True, exist_ok=True)
    (target / "src" / candidate.source_file).write_text(scrubbed_source + "\n", encoding="utf-8")
    (target / "tests" / candidate.test_file).write_text(candidate.test_text + "\n", encoding="utf-8")

    checksum = compute_checksum(target)
    payload = _manifest_payload(candidate, checksum)
    MethodUnitManifest.model_validate(payload)  # 契约校验：不合格即拒绝（不写 manifest）
    (target / "manifest.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    index = Path(index_path)
    scan_methods(root)  # 全量校验（含 checksum）通过才重建
    result = build_index(root, index)  # 原子发布索引新版本
    return PublishedAsset(
        asset_ref=f"{candidate.asset_id}@{candidate.version}", version=candidate.version,
        checksum=checksum, index_revision=result.revision, path=str(target),
    )


def record_rejection(
    candidate: Candidate, review: CandidateReview, review_root: str | Path
) -> Path:
    """被拒候选留在**审查区**（不进入索引/公共库）。"""
    directory = Path(review_root) / REVIEW_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{candidate.asset_id}@{candidate.version}.json"
    path.write_text(
        json.dumps(
            {
                "asset_id": candidate.asset_id, "version": candidate.version,
                "origin_run_id": candidate.origin_run_id, "provenance": candidate.provenance,
                "approved": review.approved, "issues": review.issues,
                "scrub_findings": review.scrub_findings,
            },
            ensure_ascii=False, indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    return path


def index_meta(index_path: str | Path) -> dict:
    """读取已发布索引的元数据（供审计：index_revision/asset_count）。"""
    path = Path(index_path)
    if not is_valid_index(path):
        return {}
    return read_meta(path)


def compare_metrics(before: ReuseMetrics, after: ReuseMetrics) -> dict:
    """发布前后复用收益对比（明确测量，供人工判断是否值得回灌）。"""
    return {
        "retrieval_hit_rate": {"before": before.retrieval_hit_rate, "after": after.retrieval_hit_rate,
                               "delta": round(after.retrieval_hit_rate - before.retrieval_hit_rate, 4)},
        "compatibility_pass_rate": {"before": before.compatibility_pass_rate,
                                    "after": after.compatibility_pass_rate,
                                    "delta": round(after.compatibility_pass_rate - before.compatibility_pass_rate, 4)},
        "reuse_rate": {"before": before.reuse_rate, "after": after.reuse_rate,
                       "delta": round(after.reuse_rate - before.reuse_rate, 4)},
        "acceptance_success_rate": {"before": before.acceptance_success_rate,
                                    "after": after.acceptance_success_rate,
                                    "delta": round(after.acceptance_success_rate - before.acceptance_success_rate, 4)},
        "model_calls": {"before": before.model_calls, "after": after.model_calls,
                        "delta": after.model_calls - before.model_calls},
        "cost_micro": {"before": before.cost_micro, "after": after.cost_micro,
                       "delta": after.cost_micro - before.cost_micro},
        "duration_ms": {"before": before.duration_ms, "after": after.duration_ms,
                        "delta": after.duration_ms - before.duration_ms},
    }


def default_methods_root() -> Path:
    return REPO_ROOT / "methods"


__all__ = [
    "DEFAULT_PRIVATE_MARKERS",
    "Candidate",
    "CandidateReview",
    "PublishedAsset",
    "ReuseMetrics",
    "assert_not_agent_writable",
    "compare_metrics",
    "default_methods_root",
    "index_meta",
    "publish_candidate",
    "record_rejection",
    "review_candidate",
    "scrub_source",
    "sensitive_findings",
]
