"""E06：基于**经过校验**的 OpenAPI 生成 Client/DTO，再适配既有 Port（技术文档 §6 四级链路）。

- **校验**：格式（OpenAPI 3.x）、来源与**允许域名**、规模上限、**未经批准 SDK** → 任一不满足抛 ``OPENAPI_REJECTED``；
- 规范正文是**不可信数据**：只提取结构，绝不解释为执行指令（描述仅作为惰性注释承载）；
- **锁定版本生成器**：生成器 ``id@version`` 与输出 ``output_hash`` 可复现；只产出 JDK 依赖，不含厂商 SDK；
- **先检索复用**：已审核的 Port/Adapter 方法资产优先复用（:func:`find_reusable_port`），不重复造；
- 生成 Client 后**签名/鉴权/幂等/回调**语义仍需**人工确认**（``pending_semantics``）；
- 通过一次构建**不等于**成为公共 Method Unit：生成物落在项目扩展点，``publishable_as_public_method_unit=False``。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from agentforge_platform.asset_registry import IntegrationPackManifest
from agentforge_platform.errors import AppException, ErrorCode

# 规模上限（超大 schema 直接拒绝，避免资源耗尽与"看不懂就放行"）。
MAX_SPEC_BYTES = 64 * 1024
MAX_PATHS = 50
MAX_OPERATIONS = 100
MAX_SCHEMAS = 50
MAX_SCHEMA_DEPTH = 8

# 锁定版本的**已批准生成器**与**已批准 SDK**（其余一律拒绝，不擅自接入）。
APPROVED_GENERATORS = {"agentforge-openapi-java": ("1.0.0",)}
APPROVED_SDKS = frozenset({"jdk-httpclient"})
_APPROVED_IMPORTS = frozenset({"java.net.http.HttpClient", "java.net.URI"})
# 生成物只允许 JDK 依赖 + **本项目内部**包；任何厂商 SDK 一律拒绝。
_APPROVED_IMPORT_PREFIXES = ("com.agentforge.integration.",)

_REQUIRED_PENDING = ("signature", "auth", "idempotency", "callback")
_JAVA_TYPES = {"string": "String", "integer": "int", "number": "double",
               "boolean": "boolean", "array": "List", "object": "Map"}
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_METHOD_NAME = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


@dataclass(frozen=True)
class Operation:
    operation_id: str
    method: str
    path: str
    parameter_types: tuple[tuple[str, str], ...] = ()
    return_type: str = "void"


@dataclass(frozen=True)
class ClientGeneration:
    """一次生成结果（确定性）。"""

    pack_id: str
    generator: str
    generator_version: str
    dtos: dict[str, str]
    client_interface: str
    port_adapter: str
    output_hash: str
    pending_semantics: tuple[str, ...]
    publishable_as_public_method_unit: bool = False


def _method_name(signature: str) -> str:
    match = _METHOD_NAME.search(signature)
    return match.group(1) if match else signature.strip()


def _is_private_host(host: str) -> bool:
    lowered = host.lower()
    if lowered in {"localhost", "metadata.google.internal"} or lowered.endswith(".internal"):
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_reserved


def _url_issues(url: str, allowed_hosts: tuple[str, ...], label: str) -> list[str]:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return [f"{label} 必须是 https：{url}（拒绝非加密来源）"]
    if "@" in parsed.netloc:
        return [f"{label} 不得内嵌凭据：{url}"]
    host = (parsed.hostname or "").lower()
    if not host:
        return [f"{label} 缺少主机名：{url}"]
    if _is_private_host(host):
        return [f"{label} 指向内网/回环/元数据地址，拒绝：{host}"]
    if host not in {allowed.strip().lower() for allowed in allowed_hosts}:
        return [f"{label} 主机未在允许域名内：{host}"]
    return []


def _schema_depth(schema: object, depth: int = 0) -> int:
    if not isinstance(schema, dict):
        return depth
    deepest = depth
    for key in ("properties", "items", "additionalProperties"):
        node = schema.get(key)
        if isinstance(node, dict):  # 含 properties/additionalProperties
            children = node.values() if key == "properties" else [node]
            for child in children:
                deepest = max(deepest, _schema_depth(child, depth + 1))
    return deepest


def validate_openapi(
    text: str, *, source_url: str | None = None, allowed_hosts: tuple[str, ...] = ()
) -> list[str]:
    """校验 OpenAPI：格式/来源/允许域名/规模/未批准 SDK。返回拒绝理由（空 = 通过）。"""
    issues: list[str] = []
    if len(text.encode("utf-8")) > MAX_SPEC_BYTES:
        issues.append(f"规范过大：超过 {MAX_SPEC_BYTES} 字节上限")
        return issues  # 过大不再解析
    try:
        spec = json.loads(text)
    except ValueError:
        return ["规范不是合法 JSON（本步仅支持 JSON OpenAPI）"]
    if not isinstance(spec, dict):
        return ["规范根必须是对象"]
    version = str(spec.get("openapi") or "")
    if not version.startswith("3."):
        issues.append(f"仅支持 OpenAPI 3.x，收到：{version or '(缺失)'}")
    if not isinstance(spec.get("info"), dict) or not spec["info"].get("title"):
        issues.append("缺少 info.title")
    paths = spec.get("paths")
    if not isinstance(paths, dict) or not paths:
        issues.append("缺少 paths")
    else:
        if len(paths) > MAX_PATHS:
            issues.append(f"路径过多：{len(paths)} > {MAX_PATHS}")
        operations = sum(
            1 for item in paths.values() if isinstance(item, dict)
            for key in item if key.lower() in {"get", "post", "put", "delete", "patch"}
        )
        if operations > MAX_OPERATIONS:
            issues.append(f"操作过多：{operations} > {MAX_OPERATIONS}")

    schemas = (spec.get("components") or {}).get("schemas") if isinstance(spec.get("components"), dict) else None
    if isinstance(schemas, dict):
        if len(schemas) > MAX_SCHEMAS:
            issues.append(f"schema 过多：{len(schemas)} > {MAX_SCHEMAS}")
        for name, schema in schemas.items():
            if _schema_depth(schema) > MAX_SCHEMA_DEPTH:
                issues.append(f"schema 嵌套过深（> {MAX_SCHEMA_DEPTH}）：{name}")

    for index, server in enumerate(spec.get("servers") or []):
        if isinstance(server, dict) and server.get("url"):
            issues.extend(_url_issues(str(server["url"]), allowed_hosts, f"servers[{index}].url"))
    if source_url:
        issues.extend(_url_issues(source_url, allowed_hosts, "source_url"))

    sdk = spec.get("x-vendor-sdk")
    if sdk is not None and str(sdk) not in APPROVED_SDKS:
        issues.append(f"未经批准的 SDK：{sdk}")
    return issues


def _operations(spec: dict) -> list[Operation]:
    operations = []
    for path, item in sorted((spec.get("paths") or {}).items()):
        if not isinstance(item, dict):
            continue
        for method, operation in sorted(item.items()):
            if method.lower() not in {"get", "post", "put", "delete", "patch"}:
                continue
            if not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId") or "")
            params = tuple(
                (str(param.get("name")), _JAVA_TYPES.get(str((param.get("schema") or {}).get("type")), "String"))
                for param in (operation.get("parameters") or [])
                if isinstance(param, dict) and param.get("name")
            )
            operations.append(Operation(
                operation_id=operation_id or f"{method.lower()}{path}",
                method=method.upper(), path=path, parameter_types=params,
                return_type=_response_type(operation),
            ))
    return operations


def _response_type(operation: dict) -> str:
    responses = operation.get("responses")
    if not isinstance(responses, dict):
        return "void"
    for code in ("200", "201", "202"):
        response = responses.get(code)
        if isinstance(response, dict):
            content = response.get("content")
            if isinstance(content, dict):
                for media in content.values():
                    schema = (media or {}).get("schema") if isinstance(media, dict) else None
                    if isinstance(schema, dict):
                        ref = schema.get("$ref")
                        if isinstance(ref, str):
                            return ref.rsplit("/", 1)[-1]
                        return _JAVA_TYPES.get(str(schema.get("type")), "Map")
    return "void"


def _emit_dtos(spec: dict) -> dict[str, str]:
    schemas = (spec.get("components") or {}).get("schemas") if isinstance(spec.get("components"), dict) else {}
    dtos: dict[str, str] = {}
    for name, schema in sorted((schemas or {}).items()):
        if _NAME.fullmatch(name) is None:
            continue
        fields = []
        for prop, definition in sorted((schema.get("properties") or {}).items()):
            if _NAME.fullmatch(prop) is None or not isinstance(definition, dict):
                continue
            ref = definition.get("$ref")
            java_type = ref.rsplit("/", 1)[-1] if isinstance(ref, str) else _JAVA_TYPES.get(
                str(definition.get("type")), "String"
            )
            fields.append(f"        {java_type} {prop}")
        body = ",\n".join(fields) if fields else ""
        dtos[name] = (
            "package com.agentforge.integration.dto;\n\n"
            f"// 由 OpenAPI 生成（内部 DTO，非厂商类型）\n"
            f"public record {name}(\n{body}\n) {{}}\n"
        )
    return dtos


def _emit_client_interface(provider: str, operations: list[Operation], dtos: dict[str, str]) -> str:
    lines = [
        "package com.agentforge.integration.client;",
        "",
        "import java.net.http.HttpClient;",
        "import java.net.URI;",
        "",
        f"/** {provider} 原始 HTTP Client（已锁定版本生成器产出；只依赖 JDK）。 */",
        f"public interface {provider}ApiClient {{",
    ]
    for operation in operations:
        params = ", ".join(f"{java_type} {name}" for name, java_type in operation.parameter_types)
        lines.append(f"    {operation.return_type} {operation.operation_id}({params});")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _emit_port_adapter(pack_id: str, port_methods: list[str], operations: list[Operation],
                       dtos: dict[str, str]) -> str:
    by_id = {operation.operation_id: operation for operation in operations}
    lines = [
        "package com.agentforge.integration.adapter;",
        "",
        "import com.agentforge.integration.dto.*;",
        "",
        f"/** {pack_id} 的 Port 适配器：把已生成 Client 适配到既有 Port。 */",
        f"public final class {_camel(pack_id)}PortAdapter {{",
        "    // PENDING(人工确认)：签名 / 鉴权 / 幂等 / 回调语义——见 pending_semantics。",
    ]
    for signature in port_methods:
        method = _method_name(signature)
        operation = by_id.get(method)
        return_type = operation.return_type if operation else "void"
        params = _signature_params(signature)
        lines.append(f"    public {return_type} {method}({params}) {{")
        lines.append(f"        throw new UnsupportedOperationException(\"PENDING: {method} 语义待人工确认\");")
        lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _signature_params(signature: str) -> str:
    if "(" not in signature or ")" not in signature:
        return ""
    inner = signature[signature.index("(") + 1:signature.rindex(")")]
    return ", ".join(part.strip() for part in inner.split(",") if part.strip())


def _camel(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    return "".join(part[:1].upper() + part[1:] for part in parts if part)


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_JDK_TOKENS = frozenset(set(_JAVA_TYPES.values()) | {"String", "int", "long", "boolean", "double", "void"})


def _signature_type_tokens(signature: str) -> set[str]:
    """抽出方法签名中的**类型**标识符（返回类型 + 各参数类型），忽略方法/参数名。"""
    if "(" not in signature:
        return set()
    head, _, rest = signature.partition("(")
    params, _, _ = rest.partition(")")
    expressions = []
    head_parts = head.strip().split()
    if head_parts:
        expressions.append(head_parts[0])
    for param in params.split(","):
        parts = param.strip().split()
        if parts:
            expressions.append(parts[0])
    tokens: set[str] = set()
    for expression in expressions:
        tokens.update(_NAME.findall(expression))
    return tokens


def _port_conformance(port_methods: list[str], operations: list[Operation], dtos: dict[str, str]) -> list[str]:
    """Port 契约（结构）符合性：每个 Port 方法需有对应操作，且类型为内部类型/JDK 基础类型。"""
    issues: list[str] = []
    by_id = {operation.operation_id: operation for operation in operations}
    for signature in port_methods:
        method = _method_name(signature)
        if method not in by_id:
            issues.append(f"Port 方法无对应 OpenAPI 操作（operationId）：{method}")
            continue
        for token in _signature_type_tokens(signature):
            if token in dtos or token in _JDK_TOKENS:
                continue
            issues.append(f"Port 方法 {method} 引用了未生成/非内部类型：{token}")
    return issues


def generate_client(
    text: str,
    *,
    pack_id: str,
    port_methods: list[str],
    source_url: str | None = None,
    allowed_hosts: tuple[str, ...] = (),
    generator: str = "agentforge-openapi-java",
    generator_version: str = "1.0.0",
    sdk: str | None = None,
) -> ClientGeneration:
    """以**锁定版本**生成器产出 Client/DTO 并适配既有 Port；校验不通过抛 ``OPENAPI_REJECTED``。"""
    if generator not in APPROVED_GENERATORS or generator_version not in APPROVED_GENERATORS[generator]:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED,
            f"未经批准的生成器：{generator}@{generator_version}", status_code=422,
        )
    if sdk is not None and sdk not in APPROVED_SDKS:
        raise AppException(ErrorCode.OPENAPI_REJECTED, f"未经批准的 SDK：{sdk}", status_code=422)

    issues = validate_openapi(text, source_url=source_url, allowed_hosts=allowed_hosts)
    if issues:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED, "OpenAPI 校验未通过：" + "；".join(issues), status_code=422
        )

    spec = json.loads(text)
    dtos = _emit_dtos(spec)
    operations = _operations(spec)
    issues = _port_conformance(port_methods, operations, dtos)
    if issues:
        raise AppException(
            ErrorCode.OPENAPI_REJECTED, "生成 Client 未能适配 Port：" + "；".join(issues), status_code=422
        )

    provider = _camel(str((spec.get("info") or {}).get("title") or pack_id))
    client_interface = _emit_client_interface(provider, operations, dtos)
    port_adapter = _emit_port_adapter(pack_id, port_methods, operations, dtos)
    for source in (client_interface, port_adapter):
        for line in source.splitlines():
            if not line.startswith("import "):
                continue
            target = line[len("import "):].rstrip(";")
            if target in _APPROVED_IMPORTS or target.startswith(_APPROVED_IMPORT_PREFIXES):
                continue
            raise AppException(
                ErrorCode.OPENAPI_REJECTED, f"生成物引入未批准依赖：{line.strip()}", status_code=422
            )
    output_hash = hashlib.sha256(
        _canonical({"dtos": dtos, "client": client_interface, "adapter": port_adapter}).encode("utf-8")
    ).hexdigest()
    return ClientGeneration(
        pack_id=pack_id, generator=generator, generator_version=generator_version,
        dtos=dtos, client_interface=client_interface, port_adapter=port_adapter,
        output_hash=output_hash, pending_semantics=_REQUIRED_PENDING,
    )


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def find_reusable_port(
    packs: list[IntegrationPackManifest], *, port_methods: list[str]
) -> str | None:
    """先检索复用：返回提供**同名方法集** Port 的已审核资产 ``id@version#Port``（取最高版本；无则 None）。"""
    wanted = {_method_name(signature) for signature in port_methods}
    ordered = sorted(packs, key=lambda item: (item.id, _version_key(item.version)), reverse=True)
    for manifest in ordered:
        for port in manifest.ports:
            if {_method_name(signature) for signature in port.methods} == wanted:
                return f"{manifest.id}@{manifest.version}#{port.name}"
    return None


__all__ = [
    "APPROVED_GENERATORS",
    "APPROVED_SDKS",
    "ClientGeneration",
    "Operation",
    "find_reusable_port",
    "generate_client",
    "validate_openapi",
]
