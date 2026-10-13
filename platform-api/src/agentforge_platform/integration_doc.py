"""E07：只有第三方文档时生成**待确认** Integration Spec（技术文档 §6 四级链路第 3 级）。

- 从**不可信文档**中确定性提取操作/认证/错误/回调/外部待办，**每条都带出处**（文档 ID + 行 + 引文）；
- **不编造供应商协议**：缺认证/签名/幂等/回调等关键语义一律**保留 pending**，不猜；
- **提示注入无权改变执行权限**：文档中的"指令"只被**记录**为安全发现，绝不参与提取，更不改
  允许域名/权限/SDK；
- **未知 SDK 不自动安装**：文档提到 SDK 只产生待确认项；
- **冻结点**：:func:`freeze_integration_spec` 要求所有 pending 经**用户确认**，否则抛 ``INTEGRATION_PENDING``
  并返回问题清单（**暂停**）；缺失鉴权/回调 → 不启动 Live；
- 面向用户的**提问与解释**默认确定性模板，必要时由 **DeepSeek** 基于**脱敏结构化事实**补充
  （``render_questions(explain=…)``）；技术侧（Qwen）只可**提议**契约、编码角色只可补**授权 Adapter 文件**
  （:func:`adapter_edit_issues`），不得据原始聊天改写需求。本步不含任何真实模型调用。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from agentforge_platform.errors import AppException, ErrorCode

# 待确认主题（缺一不可冻结）。
PENDING_AUTH = "auth"
PENDING_SIGNATURE = "signature"
PENDING_IDEMPOTENCY = "idempotency"
PENDING_CALLBACK = "callback"
PENDING_ERRORS = "errors"
PENDING_SDK = "sdk"

# Adapter 只允许改这些路径（编码角色授权范围）。
ADAPTER_ALLOWED_PREFIX = "integration/adapter/"

_OPERATION = re.compile(r"\b(GET|POST|PUT|DELETE|PATCH)\s+(/[A-Za-z0-9_{}./:-]+)")
_AUTH = re.compile(r"(?i)(api[\s-]?key|x-api-key|authorization\s*:?\s*bearer|oauth2?|hmac|签名|bearer)")
_AUTH_HEADER = re.compile(r"`?(X-[A-Za-z][A-Za-z0-9-]+)`?")
_CALLBACK = re.compile(r"(?i)(webhook|callback|notify_url|回调)")
_ERROR = re.compile(r"\b(4\d\d|5\d\d)\b\s*[:\-–]?\s*([A-Za-z][\w ]{2,40})")
_ENV_TOKEN = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")
_SDK = re.compile(r"(?i)\b(sdk)\b|install\s+[`\"]?([a-z0-9][\w./-]+)")
_WRITE_METHOD = re.compile(r"\b(POST|PUT|DELETE|PATCH)\b")

# 提示注入特征：命中即**记录**，绝不应用。
_INJECTION = re.compile(
    r"(?i)(ignore (all )?(previous|prior) instructions|忽略(此前|之前|上面)?(所有)?指令|"
    r"grant (all )?(permissions?|access)|允许(访问)?(所有|任意)|open all|allow all domains?|"
    r"system\s*:|you must (now )?|execute the following|运行以下指令)"
)


@dataclass(frozen=True)
class Doc:
    """一份第三方文档（**不可信数据**）。"""

    doc_id: str
    text: str


@dataclass(frozen=True)
class Fact:
    """一条带出处的提取事实。"""

    kind: str
    value: str
    source: str  # "<doc_id>:<line>"
    quote: str


@dataclass(frozen=True)
class PendingQuestion:
    """待用户确认的问题（影响冻结）。"""

    qid: str
    topic: str
    question: str
    source: str | None = None


@dataclass(frozen=True)
class IntegrationSpecDraft:
    provider: str
    operations: tuple[Fact, ...] = ()
    auth: tuple[Fact, ...] = ()
    callbacks: tuple[Fact, ...] = ()
    errors: tuple[Fact, ...] = ()
    external: tuple[Fact, ...] = ()
    pending: tuple[PendingQuestion, ...] = ()
    injections: tuple[Fact, ...] = ()

    @property
    def is_frozen_ready(self) -> bool:
        return not self.pending


@dataclass(frozen=True)
class FrozenIntegrationSpec:
    provider: str
    operations: tuple[Fact, ...]
    auth: tuple[Fact, ...]
    callbacks: tuple[Fact, ...]
    errors: tuple[Fact, ...]
    external: tuple[Fact, ...]
    confirmations: dict[str, str] = field(default_factory=dict)
    ready_for_live: bool = False  # Live 执行归 E05，须开发者确认


def _lines(doc: Doc) -> list[tuple[int, str]]:
    return [(index + 1, line.strip()) for index, line in enumerate(doc.text.splitlines())]


def _facts(doc: Doc, pattern: re.Pattern, kind: str, *, group: int = 0) -> list[Fact]:
    facts: list[Fact] = []
    for line_number, line in _lines(doc):
        if not line or _INJECTION.search(line):
            continue  # 注入行不参与提取
        match = pattern.search(line)
        if match:
            value = match.group(group) if match.groups() else match.group(0)
            facts.append(Fact(kind=kind, value=str(value).strip(), source=f"{doc.doc_id}:{line_number}", quote=line[:200]))
    return facts


def _injections(doc: Doc) -> list[Fact]:
    return [
        Fact(kind="injection", value="prompt_injection", source=f"{doc.doc_id}:{line_number}", quote=line[:200])
        for line_number, line in _lines(doc)
        if _INJECTION.search(line)
    ]


def _dedupe(facts: Iterable[Fact]) -> tuple[Fact, ...]:
    seen: set[tuple[str, str]] = set()
    result: list[Fact] = []
    for fact in facts:
        key = (fact.kind, fact.value)
        if key in seen:
            continue
        seen.add(key)
        result.append(fact)
    return tuple(result)


def extract_integration_spec(docs: Iterable[Doc], *, provider: str) -> IntegrationSpecDraft:
    """从（多份）文档确定性提取草稿；缺失的关键语义一律保留 pending，绝不编造。"""
    operations: list[Fact] = []
    auth: list[Fact] = []
    callbacks: list[Fact] = []
    errors: list[Fact] = []
    external: list[Fact] = []
    injections: list[Fact] = []
    for doc in docs:
        injections.extend(_injections(doc))
        operations.extend(_facts(doc, _OPERATION, "operation"))
        auth.extend(_facts(doc, _AUTH, "auth"))
        auth.extend(_facts(doc, _AUTH_HEADER, "auth_header", group=1))
        callbacks.extend(_facts(doc, _CALLBACK, "callback"))
        errors.extend(_facts(doc, _ERROR, "error", group=2))
        external.extend(_facts(doc, _ENV_TOKEN, "external"))
        # 文档提到 SDK → 只产生待确认项，不自动安装
        if any(_SDK.search(line) for _, line in _lines(doc) if not _INJECTION.search(line)):
            injections.append(Fact(kind="sdk_mention", value="sdk", source=f"{doc.doc_id}:1", quote="(SDK 提及)"))

    draft = IntegrationSpecDraft(
        provider=provider,
        operations=_dedupe(operations),
        auth=_dedupe(auth),
        callbacks=_dedupe(callbacks),
        errors=_dedupe(errors),
        external=_dedupe(external),
        injections=tuple(injections),
    )
    return _with_pending(draft)


def _with_pending(draft: IntegrationSpecDraft) -> IntegrationSpecDraft:
    pending: list[PendingQuestion] = []
    source = draft.operations[0].source if draft.operations else None
    if not draft.auth:
        pending.append(PendingQuestion("q-auth", PENDING_AUTH,
                                       "该服务使用哪种认证方式（API Key / Bearer / OAuth / 签名）？", source))
    else:
        # 有认证但签名/幂等细节通常仍需确认
        pending.append(PendingQuestion("q-signature", PENDING_SIGNATURE,
                                       "请求是否要求签名/HMAC？算法、待签字段与时间戳规则是什么？", source))
    if any(_WRITE_METHOD.search(fact.value.upper()) or fact.value.upper() in
           {"POST", "PUT", "DELETE", "PATCH"} or "/subscri" in fact.value for fact in draft.operations):
        pending.append(PendingQuestion("q-idempotency", PENDING_IDEMPOTENCY,
                                       "写操作是否需幂等键？键名与去重窗口如何约定？", source))
    if draft.callbacks:
        pending.append(PendingQuestion("q-callback", PENDING_CALLBACK,
                                       "回调/Webhook 的地址字段名、重试与验签方式是什么？", source))
    if any(fact.kind == "sdk_mention" for fact in draft.injections):
        pending.append(PendingQuestion("q-sdk", PENDING_SDK,
                                       "文档提到的 SDK 未经审核；是否允许接入（不自动安装）？", None))
    if not draft.errors:
        pending.append(PendingQuestion("q-errors", PENDING_ERRORS,
                                       "该服务的主要错误码与含义（用于分类鉴权/网络/业务错误）？", source))
    return IntegrationSpecDraft(
        provider=draft.provider, operations=draft.operations, auth=draft.auth,
        callbacks=draft.callbacks, errors=draft.errors, external=draft.external,
        pending=tuple(pending), injections=draft.injections,
    )


def freeze_integration_spec(
    draft: IntegrationSpecDraft, confirmations: dict[str, str]
) -> FrozenIntegrationSpec:
    """用户确认后冻结；仍有未确认 pending 则抛 ``INTEGRATION_PENDING``（暂停 + 问题清单）。"""
    unanswered = [question for question in draft.pending if question.qid not in confirmations
                  or not str(confirmations[question.qid]).strip()]
    if unanswered:
        raise AppException(
            ErrorCode.INTEGRATION_PENDING,
            "Integration Spec 存在未确认项，已暂停：" + "；".join(
                f"{question.qid}={question.question}" for question in unanswered
            ),
            status_code=409,
        )
    return FrozenIntegrationSpec(
        provider=draft.provider, operations=draft.operations, auth=draft.auth,
        callbacks=draft.callbacks, errors=draft.errors, external=draft.external,
        confirmations=dict(confirmations), ready_for_live=False,
    )


def adapter_edit_issues(
    edited_paths: Iterable[str], *, allowed_prefix: str = ADAPTER_ALLOWED_PREFIX
) -> list[str]:
    """编码角色**只可**修改授权的 Adapter 文件；越界即报违规（不改认证模板/资产原件/锁/测试）。"""
    return [
        f"越界修改（仅允许 {allowed_prefix}** 下的 Adapter 文件）：{path}"
        for path in edited_paths
        if not str(path).lstrip("/").startswith(allowed_prefix)
    ]


def render_questions(draft: IntegrationSpecDraft, *, explain: Callable[[str], str] | None = None) -> str:
    """面向用户的问题清单：默认确定性模板；``explain``（DeepSeek）只接收**脱敏结构化事实**。"""
    lines = [f"需要你确认的第三方集成信息（{draft.provider}）："]
    for question in draft.pending:
        lines.append(f"- [{question.qid}] {question.question}")
    if not draft.pending:
        lines.append("- 无待确认项。")
    text = "\n".join(lines)
    if explain is not None:
        facts = {
            "provider": draft.provider,
            "questions": [{"qid": q.qid, "topic": q.topic} for q in draft.pending],
            "operations": [fact.value for fact in draft.operations],
        }
        text += "\n\n" + explain(json.dumps(facts, ensure_ascii=False, sort_keys=True))
    return text


def propose_contract(
    draft: IntegrationSpecDraft, *, planner: Callable[[str], str] | None = None
) -> dict:
    """技术侧**提议**（Qwen 可选）：只从**已提取事实**派生 Port 方法骨架；不得改写需求。

    ``planner`` 只接收脱敏事实并返回补充建议文本（记入 ``planner_note``）；无论是否提供，
    事实字段均**只来自文档提取**，不据原始聊天改写。
    """
    methods = [
        {"operation": fact.value, "port_method": _port_method_name(fact.value)}
        for fact in draft.operations
    ]
    proposal: dict = {"provider": draft.provider, "methods": methods,
                      "pending": [q.qid for q in draft.pending]}
    if planner is not None:
        proposal["planner_note"] = planner(
            json.dumps({"provider": draft.provider, "operations": [f.value for f in draft.operations]},
                       ensure_ascii=False, sort_keys=True)
        )
    return proposal


def _port_method_name(operation: str) -> str:
    parts = operation.split()
    if len(parts) >= 2:
        tokens = re.findall(r"[A-Za-z]+", parts[-1])
        if tokens:
            return "".join(token.lower() for token in tokens).capitalize() or "call"
    return "call"


__all__ = [
    "ADAPTER_ALLOWED_PREFIX",
    "Doc",
    "Fact",
    "FrozenIntegrationSpec",
    "IntegrationSpecDraft",
    "PendingQuestion",
    "adapter_edit_issues",
    "extract_integration_spec",
    "freeze_integration_spec",
    "propose_contract",
    "render_questions",
]
