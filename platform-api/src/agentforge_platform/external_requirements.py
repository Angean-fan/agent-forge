"""E03：External Requirement 结构化声明与配置指南生成（技术文档 §4/§6）。

- 从 Integration Pack manifest 汇总用户需**申请/填写**的结构化待办（账号/标识/密钥/回调/白名单等）；
- **确定性**生成 ``.env.example`` 与 ``EXTERNAL_SERVICES.md``：申请什么—在哪里申请—填写哪里—如何验证；
- 环境变量与代码配置键 **1:1 绑定**；同名变量语义冲突 **拒绝**（一致的重复去重）；
- **秘密扫描**：秘密字段必须留空；真实凭据绝不进入产物/日志；
- 面向用户的通俗说明默认**确定性模板**；仅在需要时由 **DeepSeek** 基于**已脱敏结构化事实**补充，
  不把技术模型（Qwen）原始输出直接面向用户，也不在无凭据时调用任何模型。
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from agentforge_platform.asset_registry import IntegrationPackManifest
from agentforge_platform.errors import AppException, ErrorCode

_SECRET_ASSIGN = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\S.*)$")
_SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE|CREDENTIAL|ACCESS_KEY|APIKEY)", re.IGNORECASE)
_PEM_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.")
_AWS_KEY = re.compile(r"\bAKIA[0-9A-Z]{16}\b")

# 非秘密变量的**占位示例值**（保留域/示例串，绝不使用真实值）。
_EXAMPLE_BY_KIND = {
    "base_url": "https://example.invalid/",
    "callback_url": "https://example.invalid/callback",
    "allowlist": "127.0.0.1",
    "app_id": "example-app-id",
}
_DEFAULT_EXAMPLE = "CHANGE_ME"


@dataclass(frozen=True)
class ExternalRequirement:
    """一条外部待办（脱敏；不含任何真实值）。"""

    requirement_id: str
    integration_ref: str
    name: str
    kind: str
    secret: bool
    required: bool
    required_in_modes: tuple[str, ...]
    env_name: str
    config_path: str
    target_file: str
    description: str
    acquisition_url: str
    acquisition_steps: tuple[str, ...]
    validation_rule: str
    docs_verified_at: str

    def _signature(self) -> tuple:
        return (
            self.kind, self.secret, self.required, self.required_in_modes,
            self.config_path, self.target_file,
        )


@dataclass(frozen=True)
class ExternalDocs:
    """生成产物（脱敏）：结构化待办 + `.env.example` + `EXTERNAL_SERVICES.md` + 扫描结论。"""

    requirements: list[ExternalRequirement]
    binding_issues: list[str]
    env_example: str
    external_services_md: str
    secret_findings: list[str]


def _local_id(item, index: int) -> str:
    return item.requirement_id or str(index)


def collect_requirements(
    packs: Iterable[IntegrationPackManifest],
) -> list[ExternalRequirement]:
    """汇总各 pack 的外部待办；同名变量语义冲突抛 ``ASSET_CONFLICT``，一致的重复去重。"""
    requirements: list[ExternalRequirement] = []
    seen: dict[str, tuple] = {}
    seen_ref: dict[str, str] = {}
    for manifest in packs:
        ref = f"{manifest.id}@{manifest.version}"
        for index, item in enumerate(manifest.config):
            requirement = ExternalRequirement(
                requirement_id=f"{ref}#{_local_id(item, index)}",
                integration_ref=ref,
                name=item.name or item.env_name,
                kind=item.kind,
                secret=item.secret,
                required=item.required,
                required_in_modes=tuple(item.required_in_modes),
                env_name=item.env_name,
                config_path=item.config_path,
                target_file=item.target_file,
                description=item.description,
                acquisition_url=item.acquisition_url,
                acquisition_steps=tuple(item.acquisition_steps),
                validation_rule=item.validation_rule,
                docs_verified_at=item.docs_verified_at,
            )
            if item.env_name in seen:
                if seen[item.env_name] != requirement._signature():
                    raise AppException(
                        ErrorCode.ASSET_CONFLICT,
                        f"同名环境变量语义冲突：{item.env_name}"
                        f"（{seen_ref[item.env_name]} vs {ref}）",
                        status_code=422,
                    )
                continue  # 语义一致 → 去重
            seen[item.env_name] = requirement._signature()
            seen_ref[item.env_name] = ref
            requirements.append(requirement)
    requirements.sort(key=lambda requirement: requirement.env_name)
    return requirements


def binding_issues(requirements: list[ExternalRequirement]) -> list[str]:
    """环境变量 ↔ 代码配置键 1:1 绑定校验（同一配置键不得被多个变量占用）。"""
    issues: list[str] = []
    paths: dict[str, str] = {}
    for requirement in requirements:
        if not requirement.env_name:
            issues.append(f"{requirement.integration_ref} 存在空 env_name")
        if requirement.config_path:
            owner = paths.get(requirement.config_path)
            if owner is not None:
                issues.append(f"配置键重复：{requirement.config_path}（{owner} 与 {requirement.env_name}）")
            else:
                paths[requirement.config_path] = requirement.env_name
    return issues


def _example_value(requirement: ExternalRequirement) -> str:
    return _EXAMPLE_BY_KIND.get(requirement.kind, _DEFAULT_EXAMPLE)


def generate_env_example(requirements: list[ExternalRequirement]) -> str:
    """确定性生成 `.env.example`：秘密留空，非秘密填保留域占位值。"""
    lines = [
        "# AgentForge 生成的 .env.example —— 由 integration pack manifest 汇总，请勿写入真实值。",
        "# 秘密字段一律留空；真实凭据仅在部署环境/Secret Store 注入，禁止提交到仓库。",
        "",
    ]
    for requirement in sorted(requirements, key=lambda item: item.env_name):
        modes = ",".join(requirement.required_in_modes) or "none"
        lines.append(
            f"# {requirement.name} | kind={requirement.kind} | "
            f"秘密={'是' if requirement.secret else '否'} | 必需模式={modes}"
        )
        value = "" if requirement.secret else _example_value(requirement)
        lines.append(f"{requirement.env_name}={value}")
    return "\n".join(lines) + "\n"


def generate_external_services_md(requirements: list[ExternalRequirement]) -> str:
    """确定性生成 `EXTERNAL_SERVICES.md`：申请什么—在哪里申请—填写哪里—如何验证。"""
    parts = [
        "# 外部服务与配置指南",
        "",
        "> 由 AgentForge 从 integration pack manifest 确定性生成；真实凭据绝不写入本文件。",
        "",
    ]
    refs = sorted({requirement.integration_ref for requirement in requirements})
    for ref in refs:
        parts.append(f"## {ref}")
        parts.append("")
        for requirement in sorted(
            (item for item in requirements if item.integration_ref == ref),
            key=lambda item: item.env_name,
        ):
            parts.append(f"### {requirement.env_name} — {requirement.name}")
            detail = f"（{requirement.kind}）"
            if requirement.description:
                detail += f" — {requirement.description}"
            parts.append(f"- 申请什么：{requirement.name} {detail}")
            if requirement.acquisition_url:
                parts.append(f"- 在哪里申请：{requirement.acquisition_url}")
            for step in requirement.acquisition_steps:
                parts.append(f"  - {step}")
            where = f"{requirement.target_file} 的 `{requirement.env_name}`"
            if requirement.config_path:
                where += f"；代码配置键 `{requirement.config_path}`"
            parts.append(f"- 填写哪里：{where}")
            verify = requirement.validation_rule or "按服务文档校验"
            if requirement.docs_verified_at:
                verify += f"（文档核验：{requirement.docs_verified_at}）"
            parts.append(f"- 如何验证：{verify}")
            parts.append(
                f"- 秘密：{'是' if requirement.secret else '否'}　"
                f"必需：{'是' if requirement.required else '否'}　"
                f"必需模式：{','.join(requirement.required_in_modes) or '无'}"
            )
            parts.append("")
    parts.extend([
        "## 如何注入真实凭据",
        "",
        "- 平台**不收集**真实第三方密钥；请在部署/本地环境的 Secret Store 注入上列变量。",
        "- 秘密变量在 `.env.example` 中留空；真实 `.env` 加入忽略规则且不进入 ZIP。",
    ])
    return "\n".join(parts) + "\n"


def scan_for_secrets(text: str) -> list[str]:
    """扫描疑似真实秘密：秘密命名变量带非空值、PEM 私钥、JWT、AWS Key。"""
    findings: list[str] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        match = _SECRET_ASSIGN.match(line)
        if match and _SECRET_NAME.search(match.group(1)) and match.group(2).strip():
            findings.append(f"第 {line_number} 行：秘密变量 {match.group(1)} 带非空值")
    if _PEM_PRIVATE_KEY.search(text):
        findings.append("包含 PEM 私钥块")
    if _JWT.search(text):
        findings.append("包含疑似 JWT")
    if _AWS_KEY.search(text):
        findings.append("包含疑似 AWS Access Key")
    return findings


def build_docs(packs: Iterable[IntegrationPackManifest]) -> ExternalDocs:
    """汇总 → 生成产物 → 绑定/秘密扫描（全部确定性，无模型调用）。"""
    requirements = collect_requirements(packs)
    env_example = generate_env_example(requirements)
    external_services_md = generate_external_services_md(requirements)
    findings = scan_for_secrets(env_example) + scan_for_secrets(external_services_md)
    return ExternalDocs(
        requirements=requirements,
        binding_issues=binding_issues(requirements),
        env_example=env_example,
        external_services_md=external_services_md,
        secret_findings=findings,
    )


def require_clean_docs(docs: ExternalDocs) -> ExternalDocs:
    """绑定不合法或秘密扫描命中即拒（``ASSET_CONFLICT``）。"""
    issues = docs.binding_issues + docs.secret_findings
    if issues:
        raise AppException(
            ErrorCode.ASSET_CONFLICT,
            "外部配置指南未通过校验：" + "；".join(issues),
            status_code=422,
        )
    return docs


def render_user_explanation(
    docs: ExternalDocs, *, explain: Callable[[str], str] | None = None
) -> str:
    """面向用户的说明：默认确定性模板；``explain`` 仅在需要通俗解释时由 DeepSeek 补充。

    ``explain`` 只接收**脱敏结构化事实** JSON，必须只复述事实，不得改事实/权限/费用/检查状态；
    不在此处调用任何模型——无凭据时保持确定性输出。
    """
    facts = {
        "services": sorted({requirement.integration_ref for requirement in docs.requirements}),
        "variables": [
            {
                "env_name": requirement.env_name,
                "secret": requirement.secret,
                "required": requirement.required,
                "required_in_modes": list(requirement.required_in_modes),
            }
            for requirement in docs.requirements
        ],
    }
    lines = ["本次集成需要配置的外部事项："]
    for requirement in docs.requirements:
        secret = "（秘密，留空）" if requirement.secret else ""
        lines.append(f"- {requirement.env_name}{secret}：{requirement.name}")
    if not docs.requirements:
        lines.append("- 无")
    summary = "\n".join(lines)

    if explain is not None:
        payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
        summary += "\n\n" + explain(payload)
    return summary


def main(argv: list[str]) -> int:
    from agentforge_platform.asset_registry import integration_registry

    registry = integration_registry()
    asset_id = argv[0] if argv else "weather-lookup"
    version = argv[1] if len(argv) > 1 else max(
        (manifest.version for manifest in registry.all() if manifest.id == asset_id),
        default="",
    )
    manifest, _ = registry.get("integration-pack", asset_id, version)
    docs = build_docs([manifest])
    print(docs.env_example)
    print(docs.external_services_md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
