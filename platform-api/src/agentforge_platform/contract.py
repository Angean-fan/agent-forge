"""业务契约编译器（M10）。

把已确认的 ``SoftwareSpecification`` 经**确定性规则**编译为可机器校验的契约：
数据库表/列/约束、REST 路由、DTO、权限矩阵、错误码，并生成稳定 ``contract_hash``。

本步只产出契约，不生成任何 CRUD 业务代码；命名冲突或不受支持的字段类型会抛出
可解释的 ``ContractError``，不做静默裁剪。
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.spec import SoftwareSpecification
from agentforge_platform.spec_service import content_hash

CONTRACT_SCHEMA_VERSION = "0.1"
MVP_ENTITY_LIMIT = 1

_STRICT = ConfigDict(extra="forbid")

# 规格字段类型 → (SQL 类型, JSON 类型)。白名单之外一律拒绝。
_TYPE_MAP: dict[str, tuple[str, str]] = {
    "long": ("BIGINT", "number"),
    "int": ("INT", "number"),
    "integer": ("INT", "number"),
    "string": ("VARCHAR(255)", "string"),
    "text": ("TEXT", "string"),
    "bool": ("BOOLEAN", "boolean"),
    "boolean": ("BOOLEAN", "boolean"),
    "datetime": ("DATETIME", "string"),
    "date": ("DATE", "string"),
    "decimal": ("DECIMAL(18,2)", "number"),
}

_ERROR_CODES = [
    ("VALIDATION_ERROR", 400, "请求参数不合法"),
    ("UNAUTHORIZED", 401, "未认证"),
    ("FORBIDDEN", 403, "无权访问"),
    ("NOT_FOUND", 404, "资源不存在"),
    ("CONFLICT", 409, "资源冲突"),
    ("INTERNAL_ERROR", 500, "服务器内部错误"),
]


class ContractError(AppException):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(code, message, status_code=422, retryable=False)


class ColumnContract(BaseModel):
    model_config = _STRICT

    name: str
    sql_type: str
    nullable: bool
    primary_key: bool = False
    unique: bool = False


class TableContract(BaseModel):
    model_config = _STRICT

    table: str
    columns: list[ColumnContract]
    unique_constraints: list[list[str]] = []
    indexes: list[list[str]] = []


class FieldContract(BaseModel):
    model_config = _STRICT

    name: str
    json_type: str
    required: bool


class DtoContract(BaseModel):
    model_config = _STRICT

    name: str
    fields: list[FieldContract]


class RouteContract(BaseModel):
    model_config = _STRICT

    operation: str
    method: str
    path: str
    request_dto: str | None
    response_dto: str
    response_is_list: bool = False
    allowed_roles: list[str]


class RolePermission(BaseModel):
    model_config = _STRICT

    role: str
    actions: list[str]


class PermissionMatrix(BaseModel):
    model_config = _STRICT

    entity: str
    roles: list[RolePermission]


class ErrorCodeContract(BaseModel):
    model_config = _STRICT

    code: str
    http_status: int
    description: str


class BusinessContract(BaseModel):
    model_config = _STRICT

    schema_version: str
    project_type: str
    spec_hash: str
    profile_ref: str
    tables: list[TableContract]
    routes: list[RouteContract]
    dtos: list[DtoContract]
    permissions: list[PermissionMatrix]
    error_codes: list[ErrorCodeContract]
    contract_hash: str


def _canonical(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def contract_hash_of(contract: BusinessContract) -> str:
    payload = contract.model_dump(mode="json")
    payload.pop("contract_hash", None)
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def _snake(name: str) -> str:
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    step2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1)
    return step2.lower()


def _sql_type(entity_name: str, field_name: str, field_type: str) -> tuple[str, str]:
    mapped = _TYPE_MAP.get(field_type)
    if mapped is None:
        raise ContractError(
            ErrorCode.CONTRACT_UNSUPPORTED,
            f"不支持的字段类型 '{field_type}'（实体 {entity_name}.{field_name}）",
        )
    return mapped


def _build_routes(entity_name: str, plural: str, permissions: list[RolePermission]) -> list[RouteContract]:
    def roles_for(action: str) -> list[str]:
        return sorted(p.role for p in permissions if action in p.actions)

    view = f"{entity_name}View"
    return [
        RouteContract(
            operation="list", method="GET", path=f"/api/{plural}",
            request_dto=None, response_dto=view, response_is_list=True,
            allowed_roles=roles_for("read")),
        RouteContract(
            operation="create", method="POST", path=f"/api/{plural}",
            request_dto=f"{entity_name}CreateRequest", response_dto=view,
            allowed_roles=roles_for("create")),
        RouteContract(
            operation="get", method="GET", path=f"/api/{plural}/{{id}}",
            request_dto=None, response_dto=view, allowed_roles=roles_for("read")),
        RouteContract(
            operation="update", method="PUT", path=f"/api/{plural}/{{id}}",
            request_dto=f"{entity_name}UpdateRequest", response_dto=view,
            allowed_roles=roles_for("update")),
        RouteContract(
            operation="delete", method="DELETE", path=f"/api/{plural}/{{id}}",
            request_dto=None, response_dto=view, allowed_roles=roles_for("delete")),
    ]


def compile_contract(
    spec: SoftwareSpecification, *, profile_ref: str, spec_hash: str
) -> BusinessContract:
    if len(spec.entities) != MVP_ENTITY_LIMIT:
        raise ContractError(
            ErrorCode.CONTRACT_UNSUPPORTED,
            f"MVP 仅支持 {MVP_ENTITY_LIMIT} 个业务实体，收到 {len(spec.entities)} 个",
        )
    entity = spec.entities[0]
    table_name = _snake(entity.name)

    columns: list[ColumnContract] = []
    seen: dict[str, str] = {}
    for field in entity.fields:
        column = _snake(field.name)
        if column in seen:
            raise ContractError(
                ErrorCode.CONTRACT_CONFLICT,
                f"实体 {entity.name} 字段名冲突：'{field.name}' 与 '{seen[column]}' 均映射为列 '{column}'",
            )
        seen[column] = field.name
        sql_type, _ = _sql_type(entity.name, field.name, field.type)
        is_pk = column == "id"
        columns.append(
            ColumnContract(
                name=column,
                sql_type=sql_type,
                nullable=(not field.required) and not is_pk,
                primary_key=is_pk,
            )
        )
    if not any(c.primary_key for c in columns):
        raise ContractError(
            ErrorCode.CONTRACT_UNSUPPORTED, f"实体 {entity.name} 缺少主键列 'id'"
        )

    # 权限矩阵（角色去重，冲突即拒绝）
    seen_roles: set[str] = set()
    role_permissions: list[RolePermission] = []
    for permission in spec.permissions:
        if permission.role in seen_roles:
            raise ContractError(
                ErrorCode.CONTRACT_CONFLICT, f"角色 '{permission.role}' 的权限重复定义"
            )
        seen_roles.add(permission.role)
        role_permissions.append(
            RolePermission(role=permission.role, actions=sorted(set(permission.actions)))
        )
    role_permissions.sort(key=lambda p: p.role)

    plural = f"{table_name}s"
    routes = _build_routes(entity.name, plural, role_permissions)

    all_fields = [
        FieldContract(
            name=_snake(f.name),
            json_type=_sql_type(entity.name, f.name, f.type)[1],
            required=bool(f.required),
        )
        for f in entity.fields
    ]
    # 主键/时间戳/归属列由服务端管理，不出现在 create/update 请求 DTO 中（防止伪造归属）。
    server_managed = {"id", "created_at", "owner_id"}
    view_fields = all_fields
    writable_fields = [f for f in all_fields if f.name not in server_managed]
    dtos = [
        DtoContract(name=f"{entity.name}CreateRequest", fields=writable_fields),
        DtoContract(name=f"{entity.name}UpdateRequest", fields=writable_fields),
        DtoContract(name=f"{entity.name}View", fields=view_fields),
    ]

    contract = BusinessContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        project_type=spec.project_type,
        spec_hash=spec_hash,
        profile_ref=profile_ref,
        tables=[
            TableContract(
                table=table_name,
                columns=columns,
                unique_constraints=[],
                indexes=[[c.name] for c in columns if c.name.endswith("_id")],
            )
        ],
        routes=routes,
        dtos=dtos,
        permissions=[PermissionMatrix(entity=entity.name, roles=role_permissions)],
        error_codes=[
            ErrorCodeContract(code=code, http_status=status, description=desc)
            for code, status, desc in _ERROR_CODES
        ],
        contract_hash="",
    )
    return contract.model_copy(update={"contract_hash": contract_hash_of(contract)})


def compile_from_spec(spec: SoftwareSpecification) -> BusinessContract:
    profile = spec.technical_profile
    return compile_contract(
        spec,
        profile_ref=f"{profile.profile_id}@{profile.profile_version}",
        spec_hash=content_hash(spec),
    )


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python -m agentforge_platform.contract <spec.json>", file=sys.stderr)
        return 2
    spec = SoftwareSpecification.model_validate_json(
        Path(argv[0]).read_text(encoding="utf-8")
    )
    print(compile_from_spec(spec).model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
