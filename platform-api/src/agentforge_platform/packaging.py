"""M24 确定性交付报告与打包。

为 run 产出源码 ZIP 与 `spec.json`/`asset-lock.json`/`verification.json`/
`cost-report.json`/`EXTERNAL_SERVICES.md`/接口说明；排除 `.env`、密钥、依赖与构建缓存、日志。
带可配置的文件数/字节/ZIP 上限；超限或扫描到密钥即中止，仅清理本 run 暂存文件并保留失败记录。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from agentforge_platform.budget_pg import cost_by_department
from agentforge_platform.contract import BusinessContract
from agentforge_platform.models import ModelCall
from agentforge_platform.resolver import AssetLock
from agentforge_platform.spec import SoftwareSpecification

_EXCLUDED_NAMES = {
    ".env", "node_modules", "target", "dist", "build", "__pycache__", ".git",
    ".venv", ".DS_Store", "Thumbs.db", "logs",
}
_EXCLUDED_SUFFIXES = (".log", ".zip")
_SECRET_PATTERNS = (
    re.compile(rb"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)
VERIFY_REQUIRED = ("static_manifest", "backend_test", "backend_build", "frontend_build")
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 200 * 1024 * 1024
DEFAULT_MAX_ZIP_BYTES = 200 * 1024 * 1024
ZIP_NAME = "source.zip"


@dataclass(frozen=True)
class PackageLimits:
    max_files: int
    max_total_bytes: int
    max_zip_bytes: int


@dataclass(frozen=True)
class PackageResult:
    zip_path: str
    file_count: int
    total_bytes: int
    zip_bytes: int
    zip_sha256: str
    manifest_hash: str
    verified: bool


class PackagingError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def package_limits() -> PackageLimits:
    return PackageLimits(
        max_files=int(os.environ.get("AGENTFORGE_PACKAGE_MAX_FILES", str(DEFAULT_MAX_FILES))),
        max_total_bytes=int(os.environ.get("AGENTFORGE_PACKAGE_MAX_BYTES", str(DEFAULT_MAX_BYTES))),
        max_zip_bytes=int(os.environ.get("AGENTFORGE_PACKAGE_MAX_ZIP_BYTES", str(DEFAULT_MAX_ZIP_BYTES))),
    )


def _is_excluded(relative: Path) -> bool:
    if any(part in _EXCLUDED_NAMES for part in relative.parts):
        return True
    if relative.name.startswith(".env.") and relative.name != ".env.example":
        return True
    return relative.name.endswith(_EXCLUDED_SUFFIXES)


def collect_files(run_dir: Path, limits: PackageLimits) -> tuple[list[Path], int]:
    files: list[Path] = []
    total = 0
    for path in sorted(run_dir.rglob("*")):
        relative = path.relative_to(run_dir)
        # 先按排除规则跳过（node_modules/target/dist/.git 等本就不打包）；
        # 否则 node_modules 内由 npm 生成的符号链接会误判为越界链接而使整个 run 失败。
        if _is_excluded(relative):
            continue
        if path.is_symlink():
            raise PackagingError("UNSAFE_PACKAGE_PATH", f"符号链接不可打包：{relative}")
        if not path.is_file():
            continue
        files.append(path)
        total += path.stat().st_size
        if len(files) > limits.max_files:
            raise PackagingError("PACKAGE_TOO_LARGE", f"文件数超过上限 {limits.max_files}")
        if total > limits.max_total_bytes:
            raise PackagingError("PACKAGE_TOO_LARGE", f"总字节超过上限 {limits.max_total_bytes}")
    for path in files:
        with path.open("rb") as stream:
            tail = b""
            for chunk in iter(lambda: stream.read(65536), b""):
                data = tail + chunk
                if any(pattern.search(data) for pattern in _SECRET_PATTERNS):
                    raise PackagingError(
                        "SECRET_DETECTED", f"疑似密钥：{path.relative_to(run_dir).as_posix()}"
                    )
                tail = data[-128:]
    return files, total


def verification_passed(verification: dict, acceptance: dict) -> bool:
    checks = {check.get("name"): check.get("status") for check in verification.get("checks", [])}
    return (
        verification.get("overall") == "PASS"
        and acceptance.get("overall") == "PASS"
        and all(checks.get(name) == "PASS" for name in VERIFY_REQUIRED)
    )


def _api_doc(contract: BusinessContract) -> str:
    lines = ["# 接口说明", "", "| 方法 | 路径 | 操作 | 允许角色 |", "|---|---|---|---|"]
    for route in contract.routes:
        lines.append(
            f"| {route.method} | {route.path} | {route.operation} | {', '.join(route.allowed_roles) or '（无）'} |"
        )
    lines += ["", f"- profile：`{contract.profile_ref}`", f"- contract_hash：`{contract.contract_hash}`", ""]
    return "\n".join(lines)


def build_verification(
    verification: dict, acceptance: dict, *, mysql_digest: str | None = None
) -> dict:
    verified = verification_passed(verification, acceptance)
    return {
        "validation_scope": "MVP",
        "verified": verified,
        "label": "已验证" if verified else "未验证",
        "runtime": {
            "verify_overall": verification.get("overall"),
            "accept_overall": acceptance.get("overall"),
            "mysql_digest": mysql_digest,
        },
        "checks": verification.get("checks", []),
        "acceptance_cases": acceptance.get("cases", []),
    }


def build_cost_report(db: Session, *, project_id: int, run_id: int) -> dict:
    """R20：分层如实报表——访谈期（生成前草稿）/ run 级（空 task）/ 各 task，并按模型/模式/强度/币种/定价版本/未知状态分列。"""

    def bucket(*, by_run: int | None, task_null: bool = False, task_id: str | None = None) -> dict:
        conditions = [ModelCall.project_id == project_id]
        conditions.append(ModelCall.run_id == by_run if by_run is not None else ModelCall.run_id.is_(None))
        if task_null:
            conditions.append(ModelCall.task_id.is_(None))
        elif task_id is not None:
            conditions.append(ModelCall.task_id == task_id)
        rows = db.scalars(select(ModelCall).where(*conditions)).all()

        by_model: dict[str, dict] = {}
        input_tokens = output_tokens = settled = unknown = 0
        for row in rows:
            input_tokens += int(row.input_tokens or 0)
            output_tokens += int(row.output_tokens or 0)
            if row.status == "SETTLED":
                settled += int(row.cost_micro or 0)
            elif row.status != "RELEASED":
                unknown += 1
            entry = by_model.setdefault(row.model, {
                "provider": row.provider, "model_role": row.model_role,
                "calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_micro": 0,
                "unknown_calls": 0, "price_version": row.price_version,
                "price_currency": row.price_currency, "modes": set(), "strengths": set(),
            })
            entry["calls"] += 1
            entry["input_tokens"] += int(row.input_tokens or 0)
            entry["output_tokens"] += int(row.output_tokens or 0)
            if row.status == "SETTLED":
                entry["cost_micro"] += int(row.cost_micro or 0)
            elif row.status != "RELEASED":
                entry["unknown_calls"] += 1
            entry["modes"].add(row.thinking_mode)
            entry["strengths"].add(row.reasoning_effort)
        for entry in by_model.values():
            entry["modes"] = sorted(m for m in entry["modes"] if m)
            entry["strengths"] = sorted(s for s in entry["strengths"] if s)
        return {
            "calls": len(rows),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_micro": settled,
            "unknown_calls": unknown,
            "by_model": by_model,
        }

    run_rows = db.scalars(
        select(ModelCall.task_id).where(ModelCall.project_id == project_id, ModelCall.run_id == run_id,
                                        ModelCall.task_id.is_not(None))
    ).all()
    by_task = {task_id: bucket(by_run=run_id, task_id=task_id) for task_id in sorted(set(run_rows))}
    draft = bucket(by_run=None)
    return {
        "currency_unit": "micro_yuan (1 元 = 1_000_000 微)",
        "price_note": "非人民币定价按锁定汇率快照折算并记录时间；缺核定价格/汇率时成本为 UNKNOWN 并保留预留，不以估算冒充。",
        "interview_phase": draft,   # 访谈/生成前草稿（run_id 为空）
        "draft_phase": draft,       # 兼容旧键
        "run_level": bucket(by_run=run_id, task_null=True),  # run 级（空 task，含规划/复核等）
        "by_task": by_task,
        "by_department": cost_by_department(db, run_id),
        "generation_phase": bucket(by_run=run_id),
    }


def package_run(
    run_dir: str | Path,
    *,
    spec: SoftwareSpecification,
    lock: AssetLock,
    contract: BusinessContract,
    verification: dict,
    acceptance: dict,
    cost_report: dict,
    limits: PackageLimits | None = None,
) -> PackageResult:
    run_dir = Path(run_dir).resolve()
    limits = limits or package_limits()

    (run_dir / "spec.json").write_text(spec.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (run_dir / "asset-lock.json").write_text(lock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    (run_dir / "verification.json").write_text(
        json.dumps(
            build_verification(verification, acceptance, mysql_digest=lock.mysql_image.digest),
            indent=2, ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    (run_dir / "cost-report.json").write_text(
        json.dumps(cost_report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (run_dir / "API.md").write_text(_api_doc(contract), encoding="utf-8")
    # C03：集成包装配已写入外部服务指南时保留之；否则写占位（无第三方外部服务）。
    external_services = run_dir / "EXTERNAL_SERVICES.md"
    if not external_services.is_file():
        external_services.write_text(
            "# 外部服务\n\n本 MVP 无第三方外部服务，**无须配置**。\n", encoding="utf-8"
        )

    files, total_bytes = collect_files(run_dir, limits)
    zip_path = run_dir / ZIP_NAME
    hasher = hashlib.sha256()
    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                arcname = path.relative_to(run_dir).as_posix()
                data = path.read_bytes()
                hasher.update(arcname.encode("utf-8"))
                hasher.update(b"\0")
                hasher.update(hashlib.sha256(data).digest())
                archive.writestr(arcname, data)
        if zip_path.stat().st_size > limits.max_zip_bytes:
            raise PackagingError("PACKAGE_TOO_LARGE", "ZIP 超过上限")
    except Exception:
        zip_path.unlink(missing_ok=True)  # 仅清理本 run 的暂存 ZIP，保留失败记录
        raise

    return PackageResult(
        zip_path=zip_path.name,
        file_count=len(files),
        total_bytes=total_bytes,
        zip_bytes=zip_path.stat().st_size,
        zip_sha256=hashlib.sha256(zip_path.read_bytes()).hexdigest(),
        manifest_hash=hasher.hexdigest(),
        verified=verification_passed(verification, acceptance),
    )
