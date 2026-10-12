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
from agentforge_platform.spec import MAX_BUSINESS_ENTITIES, SoftwareSpecification
from agentforge_platform.spec_service import content_hash

CONTRACT_SCHEMA_VERSION = "0.1"

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


def plural_of(table_name: str) -> str:
    """表名复数化（用于 REST 路径）：``y→ies``、``s/x/z/ch/sh→es``，否则 ``+s``。"""
    if table_name.endswith("y") and table_name[-2:-1] not in "aeiou":
        return table_name[:-1] + "ies"
    if table_name.endswith(("s", "x", "z", "ch", "sh")):
        return table_name + "es"
    return table_name + "s"


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


def _role_permissions(spec: SoftwareSpecification) -> list[RolePermission]:
    """全局权限矩阵（角色去重，冲突即拒绝）；未做实体覆盖时的默认角色集。"""
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
    return role_permissions


def _validate_entity_permissions(
    spec: SoftwareSpecification, by_name: dict[str, object]
) -> None:
    for entry in spec.entity_permissions:
        if entry.get("entity") not in by_name:
            raise ContractError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"entity_permissions 引用了未知实体：{entry.get('entity')!r}"
            )


def _role_permissions_for(spec: SoftwareSpecification, entity_name: str) -> list[RolePermission]:
    """按实体解析角色权限：`entity_permissions` 覆盖该实体，否则沿用全局 permissions。"""
    entries = [e for e in spec.entity_permissions if e.get("entity") == entity_name]
    if not entries:
        return _role_permissions(spec)
    seen_roles: set[str] = set()
    role_permissions: list[RolePermission] = []
    for entry in entries:
        role = str(entry["role"])
        if role in seen_roles:
            raise ContractError(ErrorCode.CONTRACT_CONFLICT, f"实体 {entity_name} 角色 '{role}' 权限重复")
        seen_roles.add(role)
        role_permissions.append(
            RolePermission(role=role, actions=sorted(set(entry["actions"])))
        )
    role_permissions.sort(key=lambda p: p.role)
    return role_permissions


def _resolve_relations(
    spec: SoftwareSpecification,
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[list[str]]], list[str]]:
    """解析确定性关系（一对多/多对一，统一为**子实体持有父外键**）。

    返回 ``(子实体 → 外键列, 子实体 → 唯一约束, 实体拓扑序)``：``unique: true`` 的关系生成
    ``UNIQUE (父外键, owner_id)``（“同一成员-父记录最多一条”）。父实体必须先于子实体。
    """
    names = [entity.name for entity in spec.entities]
    parents_of: dict[str, set[str]] = {name: set() for name in names}
    fk_by_child: dict[str, list[dict[str, str]]] = {name: [] for name in names}
    unique_by_child: dict[str, list[list[str]]] = {name: [] for name in names}
    seen: set[tuple[str, str]] = set()
    for relation in spec.relations:
        child = relation.get("child")
        parent = relation.get("parent")
        kind = relation.get("kind", "many-to-one")
        if kind not in {"many-to-one", "one-to-many"}:
            raise ContractError(ErrorCode.CONTRACT_UNSUPPORTED, f"不支持的关系类型：{kind!r}")
        if child not in parents_of or parent not in parents_of:
            raise ContractError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"关系引用了未知实体：{child!r} -> {parent!r}"
            )
        if child == parent:
            raise ContractError(ErrorCode.CONTRACT_UNSUPPORTED, "不支持自关联")
        if (child, parent) in seen:
            continue
        seen.add((child, parent))
        parents_of[child].add(parent)
        fk_column = f"{_snake(parent)}_id"
        fk_by_child[child].append(
            {"name": fk_column, "nullable": not bool(relation.get("required", False))}
        )
        if relation.get("unique"):
            unique_by_child[child].append(sorted([fk_column, "owner_id"]))
    order: list[str] = []
    remaining = set(names)
    while remaining:
        ready = [n for n in names if n in remaining and not (parents_of[n] & remaining)]
        if not ready:
            raise ContractError(ErrorCode.CONTRACT_UNSUPPORTED, "实体关系存在环")
        for name in ready:
            remaining.discard(name)
        order.extend(ready)
    return fk_by_child, unique_by_child, order


def _validate_status_guards(
    spec: SoftwareSpecification, fk_by_child: dict[str, list[dict[str, str]]]
) -> None:
    """校验状态守卫声明：子/父实体存在、子实体经外键指向父实体、父实体有 ``status`` 列、禁止状态非空。"""
    names = {entity.name for entity in spec.entities}
    fk_columns = {name: {fk["name"] for fk in cols} for name, cols in fk_by_child.items()}
    for guard in spec.status_guards:
        child = guard.get("child")
        parent = guard.get("parent")
        blocked = guard.get("blocked_status")
        if child not in names or parent not in names:
            raise ContractError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"状态守卫引用了未知实体：{child!r}/{parent!r}"
            )
        fk_column = f"{_snake(parent)}_id"
        if fk_column not in fk_columns.get(child, set()):
            raise ContractError(
                ErrorCode.CONTRACT_UNSUPPORTED,
                f"状态守卫要求 {child} 经 {fk_column} 关联 {parent}",
            )
        if not isinstance(blocked, list) or not blocked or not all(
            isinstance(item, str) and item for item in blocked
        ):
            raise ContractError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"状态守卫 {child} 的 blocked_status 必须为非空字符串列表"
            )


def compile_contract(
    spec: SoftwareSpecification, *, profile_ref: str, spec_hash: str
) -> BusinessContract:
    if not 1 <= len(spec.entities) <= MAX_BUSINESS_ENTITIES:
        raise ContractError(
            ErrorCode.CONTRACT_UNSUPPORTED,
            f"业务实体数量需为 1—{MAX_BUSINESS_ENTITIES}，收到 {len(spec.entities)} 个",
        )
    by_name = {entity.name: entity for entity in spec.entities}
    if len(by_name) != len(spec.entities):
        raise ContractError(ErrorCode.CONTRACT_CONFLICT, "业务实体名重复")

    fk_by_child, unique_by_child, order = _resolve_relations(spec)
    _validate_status_guards(spec, fk_by_child)
    _validate_entity_permissions(spec, by_name)

    tables: list[TableContract] = []
    routes: list[RouteContract] = []
    dtos: list[DtoContract] = []
    permissions: list[PermissionMatrix] = []
    seen_tables: set[str] = set()
    seen_plurals: set[str] = set()

    for name in order:
        entity = by_name[name]
        table_name = _snake(name)
        if table_name in seen_tables:
            raise ContractError(ErrorCode.CONTRACT_CONFLICT, f"表名冲突：{table_name}")
        seen_tables.add(table_name)
        plural = plural_of(table_name)
        if plural in seen_plurals:
            raise ContractError(ErrorCode.CONTRACT_CONFLICT, f"路由路径冲突：/api/{plural}")
        seen_plurals.add(plural)
        role_permissions = _role_permissions_for(spec, name)

        # 字段 = 实体自身字段（顺序保持）+ 关系外键列（追加）。
        named = [(field.name, field.type, bool(field.required)) for field in entity.fields]
        named += [(fk["name"], "long", not fk["nullable"]) for fk in fk_by_child.get(name, [])]

        columns: list[ColumnContract] = []
        seen_columns: dict[str, str] = {}
        for field_name, field_type, required in named:
            column = _snake(field_name)
            if column in seen_columns:
                raise ContractError(
                    ErrorCode.CONTRACT_CONFLICT,
                    f"实体 {name} 字段名冲突：'{field_name}' 与 '{seen_columns[column]}' "
                    f"均映射为列 '{column}'",
                )
            seen_columns[column] = field_name
            sql_type, _ = _sql_type(name, field_name, field_type)
            is_pk = column == "id"
            columns.append(
                ColumnContract(
                    name=column,
                    sql_type=sql_type,
                    nullable=(not required) and not is_pk,
                    primary_key=is_pk,
                )
            )
        if not any(column.primary_key for column in columns):
            raise ContractError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"实体 {name} 缺少主键列 'id'"
            )
        tables.append(
            TableContract(
                table=table_name,
                columns=columns,
                unique_constraints=unique_by_child.get(name, []),
                indexes=[[column.name] for column in columns if column.name.endswith("_id")],
            )
        )
        routes.extend(_build_routes(name, plural, role_permissions))

        all_fields = [
            FieldContract(
                name=_snake(field_name),
                json_type=_sql_type(name, field_name, field_type)[1],
                required=required,
            )
            for field_name, field_type, required in named
        ]
        # 主键/时间戳/归属列由服务端管理，不出现在 create/update 请求 DTO 中（防止伪造归属）。
        server_managed = {"id", "created_at", "owner_id"}
        writable_fields = [f for f in all_fields if f.name not in server_managed]
        dtos.extend(
            [
                DtoContract(name=f"{name}CreateRequest", fields=writable_fields),
                DtoContract(name=f"{name}UpdateRequest", fields=writable_fields),
                DtoContract(name=f"{name}View", fields=all_fields),
            ]
        )
        permissions.append(PermissionMatrix(entity=name, roles=role_permissions))

    contract = BusinessContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        project_type=spec.project_type,
        spec_hash=spec_hash,
        profile_ref=profile_ref,
        tables=tables,
        routes=routes,
        dtos=dtos,
        permissions=permissions,
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
