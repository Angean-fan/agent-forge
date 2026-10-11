"""能力包加载与确定性渲染（M11）。

能力包是**可参数化的模板资产**：给定已冻结的 ``BusinessContract``，把后端 CRUD /
分页 / 搜索 / 数据归属代码渲染进基础模板，并注入所需依赖。参数只能来自经校验的
contract（不接受到模型输出或任意字符串），因此字段/命名不可被外部随意指定。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict

from agentforge_platform.contract import BusinessContract
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


class PackRenderError(AppException):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(code, message, status_code=422, retryable=False)


class RenderEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: str
    target: str


class CapabilityPackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    kind: str
    compatible_profiles: list[str]
    requires: list[str]
    provides: list[str]
    conflicts: list[str]
    parameter_schema: dict[str, str]
    owned_paths: list[str]
    dependencies: list[dict[str, str]] = []
    checks: list[str] = []
    license: str
    renders: list[RenderEntry]


def load_manifest(pack_dir: str | Path) -> CapabilityPackManifest:
    manifest_path = Path(pack_dir) / "manifest.json"
    return CapabilityPackManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def _camel(column: str) -> str:
    head, *rest = column.split("_")
    return head + "".join(part.capitalize() for part in rest)


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


def build_params(contract: BusinessContract) -> dict:
    if len(contract.tables) != 1 or len(contract.permissions) != 1:
        raise PackRenderError(ErrorCode.CONTRACT_UNSUPPORTED, "能力包仅支持单表单实体契约")

    table = contract.tables[0]
    entity_name = contract.permissions[0].entity
    plural = contract.routes[0].path.rsplit("/", 1)[-1]

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

    return {
        "package": PACKAGE,
        "package_dir": PACKAGE_DIR,
        "entity_name": entity_name,
        "entity_camel": entity_name[:1].lower() + entity_name[1:],
        "table_name": table.table,
        "plural": plural,
        "route_path": f"/api/{plural}",
        "api_path": f"/{plural}",
        "ui_path": f"/{plural}",
        "fields": fields,
        "writable_fields": writable_fields,
        "view_fields": view_fields,
        "id_pascal": _pascal(id_column.name),
        "owner_pascal": _pascal(owner_column.name),
        "search_java": _camel(search_column.name) if search_column else None,
        "search_pascal": _pascal(search_column.name) if search_column else None,
        "indexes": indexes,
        "writable_decls": _render_decls(writable_fields, "        "),
        "view_decls": _render_decls(view_fields, "        ", with_validation=False),
        "view_getters": _render_getters(view_fields, "                "),
        "columns_sql": ",\n    ".join(f["ddl"] for f in fields),
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


def _inject_dependencies(pom_path: Path, dependencies: list[dict[str, str]]) -> None:
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


def render_pack(pack_dir: str | Path, contract: BusinessContract, output_dir: str | Path) -> list[str]:
    pack_dir = Path(pack_dir)
    output_dir = Path(output_dir)
    manifest = load_manifest(pack_dir)
    params = build_params(contract)
    environment = Environment(
        loader=FileSystemLoader(pack_dir / "templates"),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )
    written: list[str] = []
    for entry in manifest.renders:
        target_relative = environment.from_string(entry.target).render(**params)
        content = environment.get_template(entry.template).render(**params)
        destination = output_dir / target_relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        written.append(str(destination))
    _inject_dependencies(output_dir / "pom.xml", manifest.dependencies)
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
        repo_root / "templates" / "backend",
        repo_root / "capability-packs" / "task-crud-backend",
        contract,
        argv[1],
    )
    print(f"rendered {len(written)} files into {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
