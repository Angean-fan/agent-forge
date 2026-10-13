"""E10：从已验收个案提炼公共方法资产并发布索引新版本。

覆盖：通用候选通过审查并**发布为不可变资产 + 原子重建索引**；复测同场景可**优先匹配复用**；
不具备通用性/来源证明/安全测试或含秘密/私有标记的候选被**拒绝且不污染公共库**；已发布版本不可原地修改；
运行中 Agent 无写权限；旧 asset-lock 仍可重放；发布前后复用收益可度量。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.asset_distillation import (
    Candidate,
    ReuseMetrics,
    assert_not_agent_writable,
    compare_metrics,
    index_meta,
    publish_candidate,
    record_rejection,
    review_candidate,
    scrub_source,
    sensitive_findings,
)
from agentforge_platform.asset_finder import CapabilityQuery, find_assets
from agentforge_platform.asset_registry import AssetRegistry, MethodUnitManifest, compute_checksum
from agentforge_platform.errors import AppException
from agentforge_platform.method_index import scan_methods, search
from agentforge_platform.resolver import AssetLock, verify_lock_hash

REPO = Path(__file__).resolve().parents[2]
KNOWN_PROFILES = {"demo-ts-mp", "demo-js-mp", "demo-ts-mb", "demo-js-mb"}

_SOURCE = """package com.agentforge.integration.util;

public final class PriorityNormalizer {
    public static String normalize(String raw) {
        if (raw == null || raw.isBlank()) {
            return "normal";
        }
        return switch (raw.trim().toLowerCase()) {
            case "urgent", "high" -> "high";
            case "low" -> "low";
            default -> "normal";
        };
    }
}
"""
_TEST = """package com.agentforge.integration.util;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.assertEquals;

class PriorityNormalizerTest {
    @Test
    void normalizesKnownValues() {
        assertEquals("high", PriorityNormalizer.normalize("URGENT"));
        assertEquals("normal", PriorityNormalizer.normalize(""));
    }
}
"""


def _candidate(**overrides) -> Candidate:
    base = {
        "asset_id": "priority-normalizer",
        "version": "1.0.0",
        "origin_run_id": 42,
        "origin_acceptance_passed": True,
        "provenance": "internal:run/42/gap/AC07 patch",
        "license": "internal",
        "generality": "纯函数式字符串归一化，无项目字段依赖，可用于任意实体的优先级映射",
        "cross_profiles": ("demo-ts-mp", "demo-js-mp"),
        "manifest": {
            "compatible_profiles": ["demo-ts-mp", "demo-js-mp"],
            "requires": [], "provides": ["priority_normalization"], "conflicts": [],
            "parameter_schema": {}, "extension_points": [],
            "owned_paths": ["backend/src/main/java/com/agentforge/integration/util/"],
            "checks": ["mvn test"], "license": "internal",
            "symbol": "com.agentforge.integration.util.PriorityNormalizer.normalize",
            "signature": "static String normalize(String raw)", "layer": "util",
            "language": "java", "framework": "spring-boot",
            "description": "优先级归一化", "tags": ["priority"],
            "source_provenance": "internal:run/42/gap/AC07 patch",
        },
        "source_file": "PriorityNormalizer.java",
        "source_text": _SOURCE,
        "test_file": "PriorityNormalizerTest.java",
        "test_text": _TEST,
    }
    base.update(overrides)
    return Candidate(**base)


# --- 审查门禁 -----------------------------------------------------------------

def test_generic_candidate_is_approved() -> None:
    review = review_candidate(_candidate(), known_profiles=KNOWN_PROFILES)

    assert review.approved is True and review.issues == [] and review.scrub_findings == []


@pytest.mark.parametrize("overrides,expected", [
    ({"origin_acceptance_passed": False}, "未通过完整 Runner 验收"),
    ({"provenance": "  "}, "可审计来源"),
    ({"license": "unknown"}, "许可证/来源不明"),
    ({"generality": ""}, "未声明通用性"),
    ({"test_text": ""}, "缺少独立"),
    ({"cross_profiles": ()}, "跨 Profile"),
    ({"cross_profiles": ("some-unknown",)}, "未覆盖任何已知 Profile"),
])
def test_non_generic_or_unproven_candidate_is_rejected(overrides: dict, expected: str) -> None:
    review = review_candidate(_candidate(**overrides), known_profiles=KNOWN_PROFILES)

    assert review.approved is False
    assert any(expected in issue for issue in review.issues)


def test_secret_or_private_candidate_is_rejected_and_scrubbed() -> None:
    dirty = _candidate(
        source_text=_SOURCE + '\nString apiKey = "sk-live-abcdef123456";\n',
        manifest={**_candidate().manifest, "owned_paths": ["com.agentforge.template.x"]},
    )
    # 私有标记放进源码文本以触发审查
    from dataclasses import replace

    dirty = replace(dirty, source_text=dirty.source_text + "// package com.agentforge.template.legacy\n")
    review = review_candidate(dirty, known_profiles=KNOWN_PROFILES)

    assert review.approved is False
    assert any("敏感或项目私有" in issue for issue in review.issues)
    assert any("秘密赋值" in finding for finding in review.scrub_findings)
    assert any("项目私有标记" in finding for finding in review.scrub_findings)
    assert "sk-live-abcdef123456" not in scrub_source(dirty.source_text)  # 密钥被抽离


# --- 发布：不可变资产 + 索引重建 + 复用 --------------------------------------

def test_publish_builds_index_and_enables_reuse(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    index = tmp_path / "idx.sqlite"
    candidate = _candidate()
    review = review_candidate(candidate, known_profiles=KNOWN_PROFILES)

    published = publish_candidate(candidate, review, root, index_path=index, approved_by="alice")

    # 不可变资产落盘且校验通过
    assert Path(published.path).is_dir()
    manifest = MethodUnitManifest.model_validate_json(
        (Path(published.path) / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest.checksum == compute_checksum(Path(published.path)) == published.checksum
    # 索引新版本（原子重建）且可检索
    meta = index_meta(index)
    assert meta.get("asset_count") == "1" and published.index_revision
    assert search("priority", path=index)

    # 复测同业务场景 → 精确匹配并复用新资产
    query = CapabilityQuery(profile="demo-ts-mp", stacks=(("java", "spring-boot"),),
                            capability_ids=("priority_normalization",), tags=(), required_acceptance_ids=())
    result = find_assets(query, registry=AssetRegistry([]), methods_root_path=root, index_path_override=index)
    assert "method-unit:priority-normalizer@1.0.0" in result.selected_refs
    assert result.match_type == "EXACT"


def test_published_version_is_immutable(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    index = tmp_path / "idx.sqlite"
    candidate = _candidate()
    review = review_candidate(candidate, known_profiles=KNOWN_PROFILES)
    publish_candidate(candidate, review, root, index_path=index, approved_by="alice")

    with pytest.raises(AppException) as error:
        publish_candidate(candidate, review, root, index_path=index, approved_by="alice")
    assert error.value.code == "ASSET_CONFLICT"


def test_rejected_candidate_does_not_pollute_library(tmp_path: Path) -> None:
    root = tmp_path / "methods"
    index = tmp_path / "idx.sqlite"
    candidate = _candidate(generality="")  # 未声明通用性
    review = review_candidate(candidate, known_profiles=KNOWN_PROFILES)

    with pytest.raises(AppException):
        publish_candidate(candidate, review, root, index_path=index, approved_by="alice")

    assert not (root / candidate.asset_id).exists()  # 未进入公共库
    plan = record_rejection(candidate, review, root)
    assert plan.is_file() and json.loads(plan.read_text())["approved"] is False


def test_agent_has_no_write_permission(tmp_path: Path) -> None:
    with pytest.raises(AppException) as error:
        assert_not_agent_writable(caller="running_agent")
    assert error.value.code == "FORBIDDEN"

    candidate = _candidate()
    review = review_candidate(candidate, known_profiles=KNOWN_PROFILES)
    with pytest.raises(AppException):
        publish_candidate(candidate, review, tmp_path / "m", index_path=tmp_path / "i.sqlite",
                          approved_by="alice", caller="running_agent")


def test_old_lock_still_replays() -> None:
    lock = AssetLock.model_validate_json(
        (REPO / "schemas" / "asset-lock.r27.sample.json").read_text(encoding="utf-8")
    )
    verify_lock_hash(lock)  # 发布新资产不改变旧锁语义


def test_metrics_compare_reports_deltas() -> None:
    before = ReuseMetrics(0.4, 0.5, 0.2, 0.7, model_calls=12, cost_micro=9000, duration_ms=5000)
    after = ReuseMetrics(0.6, 0.7, 0.4, 0.8, model_calls=8, cost_micro=6000, duration_ms=3800)

    deltas = compare_metrics(before, after)
    assert deltas["reuse_rate"]["delta"] == pytest.approx(0.2)
    assert deltas["model_calls"]["delta"] == -4
    assert deltas["cost_micro"]["delta"] == -3000


def test_sensitive_findings_detects_private_marker() -> None:
    assert sensitive_findings("package com.agentforge.template.util;")
    assert sensitive_findings(_SOURCE) == []
    assert scan_methods(REPO / "methods")  # 现有公共方法库仍可扫描（未受污染）
