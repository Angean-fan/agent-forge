"""能力包加载与确定性渲染（M11）。

能力包是**可参数化的模板资产**：给定已冻结的 ``BusinessContract``，把后端 CRUD /
分页 / 搜索 / 数据归属代码渲染进基础模板，并注入所需依赖。参数只能来自经校验的
contract（不接受到模型输出或任意字符串），因此字段/命名不可被外部随意指定。
"""

from __future__ import annotations

import re as _re
import shutil
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from agentforge_platform.asset_registry import AssetManifest
from agentforge_platform.contract import BusinessContract, TableContract, plural_of
from agentforge_platform.errors import AppException, ErrorCode

PACKAGE = "com.agentforge.template"
PACKAGE_DIR = PACKAGE.replace(".", "/")

_SQL_TO_JAVA = {
    "BIGINT": "Long",
    "INT": "Integer",
    "VARCHAR(255)": "String",
    "TEXT": "String",
    "BOOLEAN": "Boolean",
    "DATETIME": "LocalDateTime",
    "DATE": "LocalDate",
    "DECIMAL(18,2)": "BigDecimal",
}
_JAVA_IMPORTS = {
    "LocalDateTime": "java.time.LocalDateTime",
    "LocalDate": "java.time.LocalDate",
    "BigDecimal": "java.math.BigDecimal",
}
_JAVA_TO_TS = {
    "Long": "number",
    "Integer": "number",
    "String": "string",
    "Boolean": "boolean",
    "LocalDateTime": "string",
    "LocalDate": "string",
    "BigDecimal": "number",
}
_DEP_MARKER = "<!-- CAPABILITY_PACK_DEPS -->"
_OPERATION_ACTION = {
    "list": "read",
    "get": "read",
    "create": "create",
    "update": "update",
    "delete": "delete",
}
PAGINATION_MAX = 100


class PackRenderError(AppException):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(code, message, status_code=422, retryable=False)


def load_manifest(pack_dir: str | Path) -> AssetManifest:
    manifest_path = Path(pack_dir) / "manifest.json"
    return AssetManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def _camel(column: str) -> str:
    head, *rest = column.split("_")
    return head + "".join(part.capitalize() for part in rest)


def _snake(name: str) -> str:
    step1 = _re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
    step2 = _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1)
    return step2.lower()


def _pascal(column: str) -> str:
    return "".join(part.capitalize() for part in column.split("_"))


def _java_type(column: str, sql_type: str) -> str:
    java_type = _SQL_TO_JAVA.get(sql_type)
    if java_type is None:
        raise PackRenderError(
            ErrorCode.CONTRACT_UNSUPPORTED, f"不支持的列类型：{column} {sql_type}"
        )
    return java_type


def _render_decls(fields: list[dict], indent: str, *, with_validation: bool = True) -> list[str]:
    lines = []
    for index, field in enumerate(fields):
        annotation = (
            f"{field['validation_annotation']} "
            if with_validation and field["validation_annotation"]
            else ""
        )
        comma = "," if index < len(fields) - 1 else ""
        lines.append(f"{indent}{annotation}{field['java_type']} {field['java_name']}{comma}")
    return lines


def _render_getters(fields: list[dict], indent: str) -> list[str]:
    lines = []
    for index, field in enumerate(fields):
        comma = "," if index < len(fields) - 1 else ""
        lines.append(f"{indent}entity.get{field['pascal']}(){comma}")
    return lines


def _foreign_keys_sql(contract: BusinessContract, table: TableContract) -> str:
    """子表外键约束：仅当 ``<parent_snake>_id`` 前缀命中本契约的另一张表（排除 owner_id/id）。"""
    table_names = {item.table for item in contract.tables}
    clauses = [
        f",\n    FOREIGN KEY ({column.name}) REFERENCES {column.name[:-3]}(id)"
        for column in table.columns
        if column.name.endswith("_id") and column.name[:-3] in table_names
    ]
    return "".join(clauses)


def _fk_column_names(contract: BusinessContract, table: TableContract) -> list[str]:
    table_names = {item.table for item in contract.tables}
    return [
        column.name for column in table.columns
        if column.name.endswith("_id") and column.name[:-3] in table_names
    ]


_JDBC_LITERAL = {
    "BIGINT": "1L", "INT": "1", "VARCHAR(255)": '"seed"', "TEXT": '"seed"',
    "BOOLEAN": "true", "DATETIME": "java.time.LocalDateTime.now()",
    "DATE": "java.time.LocalDate.now()", "DECIMAL(18,2)": "java.math.BigDecimal.ONE",
}


def _java_literal(sql_type: str) -> str:
    return _JDBC_LITERAL.get(sql_type, '"seed"')


def _rendered_status_guards(
    contract: BusinessContract, table: TableContract, entity_name: str,
    status_guards: list[dict] | None,
) -> list[dict]:
    """把规格状态守卫渲染为服务端检查上下文（父实体/父 Mapper/外键/禁止状态）。"""
    table_names = {item.table for item in contract.tables}
    rendered: list[dict] = []
    for guard in status_guards or []:
        if guard.get("child") != entity_name:
            continue
        parent = guard.get("parent")
        parent_table = _snake(str(parent))
        if parent_table not in table_names:
            raise PackRenderError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"状态守卫父实体不存在：{parent!r}"
            )
        parent_columns = next(t for t in contract.tables if t.table == parent_table).columns
        if not any(column.name == "status" for column in parent_columns):
            raise PackRenderError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"状态守卫要求 {parent} 含 status 列"
            )
        fk_column = f"{parent_table}_id"
        if fk_column not in {column.name for column in table.columns}:
            raise PackRenderError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"状态守卫要求 {entity_name} 含外键 {fk_column}"
            )
        blocked = guard.get("blocked_status") or []
        rendered.append(
            {
                "parent_entity": str(parent),
                "parent_mapper": f"{parent}Mapper",
                "parent_field": f"{str(parent)[:1].lower()}{str(parent)[1:]}Mapper",
                "fk_pascal": _pascal(fk_column),
                "blocked_literals": ", ".join(f'"{item}"' for item in blocked),
                "blocked_first": blocked[0] if blocked else "",
            }
        )
    return rendered


def _constraint_test_context(
    contract: BusinessContract, table: TableContract, *, need_parent: bool
) -> dict | None:
    """生成工程内**数据库级**约束测试所需：父表种子 + 子外键字段（供 JdbcTemplate 插入真实父行）。"""
    if not need_parent:
        return None
    fk_columns = _fk_column_names(contract, table)
    if not fk_columns:
        return None
    parent_table_name = fk_columns[0][:-3]
    parent_table = next(t for t in contract.tables if t.table == parent_table_name)
    parent_fk = set(_fk_column_names(contract, parent_table))
    columns = [
        column for column in parent_table.columns
        if not column.primary_key and column.name != "created_at" and column.name not in parent_fk
    ]
    if not columns:
        return None
    placeholders = ", ".join("?" for _ in columns)
    args = ", ".join(
        "status" if column.name == "status" else _java_literal(column.sql_type)
        for column in columns
    )
    return {
        "fk_field": fk_columns[0],
        "parent_table": parent_table_name,
        "seed_sql": (
            f"INSERT INTO {parent_table_name} "
            f"({', '.join(column.name for column in columns)}) VALUES ({placeholders})"
        ),
        "seed_args": args,
    }


def build_params(
    contract: BusinessContract, entity_name: str | None = None, *, status_guards: list[dict] | None = None
) -> dict:
    """为某个业务实体渲染参数（R10：多实体逐实体渲染）。

    ``entity_name=None`` 时要求契约恰好单实体（MVP 语义不变）；指定实体则按该实体渲染。
    """
    entities = [matrix.entity for matrix in contract.permissions]
    if entity_name is None:
        if len(contract.tables) != 1 or len(entities) != 1:
            raise PackRenderError(
                ErrorCode.CONTRACT_UNSUPPORTED, "未指定实体时能力包仅支持单表单实体契约"
            )
        index = 0
    else:
        if entity_name not in entities:
            raise PackRenderError(
                ErrorCode.CONTRACT_UNSUPPORTED, f"契约中不存在实体 {entity_name!r}"
            )
        index = entities.index(entity_name)

    table = contract.tables[index]
    entity_name = entities[index]
    permission_matrix = contract.permissions[index]
    plural = plural_of(table.table)
    entity_routes = [
        route for route in contract.routes if route.path.startswith(f"/api/{plural}")
    ]
    if not entity_routes:
        raise PackRenderError(ErrorCode.CONTRACT_UNSUPPORTED, f"契约缺少 {entity_name} 的路由")

    create_dto = next((d for d in contract.dtos if d.name == f"{entity_name}CreateRequest"), None)
    view_dto = next((d for d in contract.dtos if d.name == f"{entity_name}View"), None)
    if create_dto is None or view_dto is None:
        raise PackRenderError(
            ErrorCode.CONTRACT_UNSUPPORTED, f"契约缺少 {entity_name} 的 DTO 定义"
        )

    column_by_name = {column.name: column for column in table.columns}
    id_column = next((column for column in table.columns if column.primary_key), None)
    owner_column = column_by_name.get("owner_id")
    if id_column is None:
        raise PackRenderError(ErrorCode.CONTRACT_UNSUPPORTED, "契约缺少主键列")
    if owner_column is None:
        raise PackRenderError(
            ErrorCode.CONTRACT_UNSUPPORTED, "契约缺少归属列 'owner_id'（数据归属校验所需）"
        )

    java_types: set[str] = set()
    fields: list[dict] = []
    for column in table.columns:
        java_type = _java_type(column.name, column.sql_type)
        java_types.add(java_type)
        if column.primary_key:
            ddl = f"{column.name} {column.sql_type} NOT NULL AUTO_INCREMENT PRIMARY KEY"
        elif column.name == "created_at":
            ddl = f"{column.name} {column.sql_type} NOT NULL DEFAULT CURRENT_TIMESTAMP"
        else:
            ddl = f"{column.name} {column.sql_type}" + ("" if column.nullable else " NOT NULL")
        fields.append(
            {
                "column": column.name,
                "sql_type": column.sql_type,
                "java_name": _camel(column.name),
                "pascal": _pascal(column.name),
                "java_type": java_type,
                "ts_type": _JAVA_TO_TS[java_type],
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "ddl": ddl,
            }
        )

    def to_dto_fields(dto) -> list[dict]:
        result = []
        for field in dto.fields:
            column = column_by_name[field.name]
            java_type = _java_type(column.name, column.sql_type)
            java_types.add(java_type)
            annotation: str | None = None
            import_name: str | None = None
            if field.required:
                if java_type == "String":
                    annotation, import_name = (
                        "@NotBlank", "jakarta.validation.constraints.NotBlank"
                    )
                else:
                    annotation, import_name = (
                        "@NotNull", "jakarta.validation.constraints.NotNull"
                    )
            result.append(
                {
                    "java_name": field.name,
                    "pascal": _pascal(field.name),
                    "java_type": java_type,
                    "ts_type": _JAVA_TO_TS[java_type],
                    "required": field.required,
                    "validation_annotation": annotation,
                    "validation_import": import_name,
                }
            )
        return result

    writable_fields = to_dto_fields(create_dto)
    view_fields = to_dto_fields(view_dto)

    search_column = next(
        (
            column
            for column in table.columns
            if column.sql_type.startswith("VARCHAR")
            and column.name not in {"id", "owner_id", "created_at"}
        ),
        None,
    )

    indexes = [group[0] for group in table.indexes if len(group) == 1]

    # 角色 → 允许操作；每条契约路由的 allowed_roles 必须与权限矩阵一致，否则拒绝装配。
    role_actions = {entry.role: set(entry.actions) for entry in permission_matrix.roles}
    op_roles: dict[str, list[str]] = {}
    for route in entity_routes:
        action = _OPERATION_ACTION[route.operation]
        expected = sorted(role for role, actions in role_actions.items() if action in actions)
        if expected != sorted(route.allowed_roles):
            raise PackRenderError(
                ErrorCode.CONTRACT_CONFLICT,
                f"路由 {route.operation} 的 allowed_roles 与权限矩阵不一致",
            )
        op_roles[route.operation] = expected
    op_roles_literals = {
        op: ", ".join(f'"{role}"' for role in roles) for op, roles in op_roles.items()
    }
    creator_role = next(
        (role for role in sorted(role_actions) if {"create", "read"} <= role_actions[role]), None
    )
    readonly_role = next(
        (
            role
            for role in sorted(role_actions)
            if "read" in role_actions[role] and "create" not in role_actions[role]
        ),
        None,
    )
    if search_column is None:
        raise PackRenderError(ErrorCode.CONTRACT_UNSUPPORTED, "Task 测试需要可搜索的字符串字段")
    if creator_role is None:
        raise PackRenderError(ErrorCode.CONTRACT_UNSUPPORTED, "Task 测试需要同时可创建和读取的角色")

    # 显式 SQL（MyBatis 非 Plus 能力包）所需：列名与 #{} 绑定串由 contract 确定性派生。
    writable_columns = [
        field for field in fields
        if not field["primary_key"] and field["column"] not in {"created_at", "owner_id"}
    ]
    id_column_name = id_column.name
    owner_column_name = owner_column.name
    fk_cols = _fk_column_names(contract, table)
    unique_with_fk = any(any(col in fk_cols for col in cols) for cols in table.unique_constraints)
    guards_here = _rendered_status_guards(contract, table, entity_name, status_guards)
    return {
        "package": PACKAGE,
        "package_dir": PACKAGE_DIR,
        "entity_name": entity_name,
        "lending_book": entity_name == "Book" and set(entities) == {"Book", "Loan"},
        "lending_loan": entity_name == "Loan" and set(entities) == {"Book", "Loan"},
        "entity_camel": entity_name[:1].lower() + entity_name[1:],
        "table_name": table.table,
        "plural": plural,
        "route_path": f"/api/{plural}",
        "api_path": f"/{plural}",
        "ui_path": f"/{plural}",
        "fields": fields,
        "entity_columns_sql": ", ".join(field["column"] for field in fields),
        "writable_columns_sql": ", ".join(field["column"] for field in writable_columns),
        "writable_values_sql": ", ".join(
            f"#{{{field['java_name']}}}" for field in writable_columns
        ),
        "writable_set_sql": ", ".join(
            f"{field['column']} = #{{{field['java_name']}}}" for field in writable_columns
        ),
        "id_column": id_column_name,
        "id_java": _camel(id_column_name),
        "id_ref": "#{" + _camel(id_column_name) + "}",
        "owner_column": owner_column_name,
        "owner_java": _camel(owner_column_name),
        "owner_ref": "#{" + _camel(owner_column_name) + "}",
        "search_column": search_column.name if search_column else None,
        # DTO 字段名为 snake；模板比较需用 snake 名，Java 变量名用 search_java（camel）。
        "search_field_name": search_column.name if search_column else None,
        "writable_fields": writable_fields,
        "view_fields": view_fields,
        "id_pascal": _pascal(id_column.name),
        "owner_pascal": _pascal(owner_column.name),
        "search_java": _camel(search_column.name) if search_column else None,
        "search_pascal": _pascal(search_column.name) if search_column else None,
        "indexes": indexes,
        "op_roles": op_roles,
        "op_roles_literals": op_roles_literals,
        "creator_role": creator_role,
        "readonly_role": readonly_role,
        "pagination_max": PAGINATION_MAX,
        "writable_decls": _render_decls(writable_fields, "        "),
        "view_decls": _render_decls(view_fields, "        ", with_validation=False),
        "view_getters": _render_getters(view_fields, "                "),
        "columns_sql": ",\n    ".join(f["ddl"] for f in fields),
        "migration_version": 2 + index,
        "foreign_keys_sql": _foreign_keys_sql(contract, table),
        "unique_constraints_sql": "".join(
            f",\n    UNIQUE ({', '.join(cols)})" for cols in table.unique_constraints
        ),
        "status_guards": guards_here,
        "has_unique_constraint": unique_with_fk,
        "child_has_status": any(column.name == "status" for column in table.columns),
        "constraint_test": _constraint_test_context(
            contract, table, need_parent=unique_with_fk or bool(guards_here)
        ),
        # DTO 字段名与列名同为 snake；外键列在生成的测试 payload 中跳过（外键列默认可空）。
        "fk_java_names": [
            field["column"] for field in fields
            if field["column"].endswith("_id")
            and field["column"][:-3] in {item.table for item in contract.tables}
        ],
        "indexes_sql": "\n".join(
            f"CREATE INDEX idx_{table.table}_{name} ON {table.table} ({name});"
            for name in indexes
        ),
        "java_imports": sorted(
            _JAVA_IMPORTS[name] for name in java_types if name in _JAVA_IMPORTS
        ),
        "writable_imports": sorted(
            {f["validation_import"] for f in writable_fields if f["validation_import"]}
        ),
    }


def inject_dependencies(pom_path: Path, dependencies: list[dict[str, str]]) -> None:
    if not dependencies:
        return
    if not pom_path.exists():
        raise PackRenderError(ErrorCode.CONTRACT_UNSUPPORTED, f"缺少 {pom_path.name}")
    text = pom_path.read_text(encoding="utf-8")
    if _DEP_MARKER not in text:
        raise PackRenderError(
            ErrorCode.CONTRACT_CONFLICT, "pom.xml 缺少能力包依赖注入点（CAPABILITY_PACK_DEPS）"
        )
    block = "\n".join(
        "    <dependency>\n"
        f"      <groupId>{dep['groupId']}</groupId>\n"
        f"      <artifactId>{dep['artifactId']}</artifactId>\n"
        f"      <version>{dep['version']}</version>\n"
        "    </dependency>"
        for dep in dependencies
    )
    pom_path.write_text(text.replace(_DEP_MARKER, f"{block}\n    {_DEP_MARKER}"), encoding="utf-8")


def _environment(pack_dir: Path) -> Environment:
    return Environment(
        loader=FileSystemLoader(pack_dir / "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def render_pack_files(
    pack_dir: str | Path,
    contract: BusinessContract,
    *,
    entity_name: str | None = None,
    status_guards: list[dict] | None = None,
) -> list[tuple[str, str]]:
    """返回 [(相对路径, 内容)]，不做任何写入（供装配器带守卫地写入）。

    ``entity_name`` 指定时按该实体渲染（R10 多实体逐实体）；``status_guards`` 为 R11 服务端状态守卫。
    """
    pack_dir = Path(pack_dir)
    params = build_params(contract, entity_name, status_guards=status_guards)
    environment = _environment(pack_dir)
    return [
        (
            environment.from_string(entry.target).render(**params),
            environment.get_template(entry.template).render(**params),
        )
        for entry in load_manifest(pack_dir).renders
    ]


def render_pack(pack_dir: str | Path, contract: BusinessContract, output_dir: str | Path) -> list[str]:
    output_dir = Path(output_dir)
    written: list[str] = []
    for target_relative, content in render_pack_files(pack_dir, contract):
        destination = output_dir / target_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        written.append(str(destination))
    inject_dependencies(output_dir / "pom.xml", load_manifest(pack_dir).dependencies)
    return written


def materialize(
    template_dir: str | Path,
    pack_dir: str | Path,
    contract: BusinessContract,
    output_dir: str | Path,
) -> list[str]:
    """把基础模板复制到输出目录并渲染能力包（不改动原模板）。"""
    output_dir = Path(output_dir)
    if output_dir.exists():
        shutil.rmtree(output_dir)
    shutil.copytree(
        template_dir,
        output_dir,
        ignore=shutil.ignore_patterns("target", "node_modules", "dist"),
    )
    return render_pack(pack_dir, contract, output_dir)


def materialize_backend(
    template_backend_dir: str | Path,
    pack_dir: str | Path,
    contract: BusinessContract,
    output_dir: str | Path,
) -> list[str]:
    return materialize(template_backend_dir, pack_dir, contract, output_dir)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: python -m agentforge_platform.capability_packs <contract.json> <output-dir>",
            file=sys.stderr,
        )
        return 2
    contract = BusinessContract.model_validate_json(Path(argv[0]).read_text(encoding="utf-8"))
    repo_root = Path(__file__).resolve().parents[3]
    written = materialize_backend(
        repo_root / "templates" / "demo-0.2" / "backend",
        repo_root / "capability-packs" / "task-crud-backend" / "0.1.1",
        contract,
        argv[1],
    )
    print(f"rendered {len(written)} files into {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
