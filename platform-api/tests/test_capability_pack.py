"""M11 后端 CRUD 能力包测试（加载 / 参数化 / 渲染 / 拒绝）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentforge_platform.capability_packs import (
    PackRenderError,
    build_params,
    load_manifest,
    materialize_backend,
)
from agentforge_platform.contract import BusinessContract, compile_from_spec
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "capability-packs" / "task-crud-backend"
TEMPLATE_BACKEND = REPO / "templates" / "backend"
SAMPLE_SPEC = REPO / "schemas" / "task-management.spec.json"


def _spec(**overrides) -> SoftwareSpecification:
    raw = json.loads(SAMPLE_SPEC.read_text(encoding="utf-8"))
    raw.update(overrides)
    return SoftwareSpecification.model_validate(raw)


def _contract(spec: SoftwareSpecification | None = None) -> BusinessContract:
    return compile_from_spec(spec or _spec())


def test_manifest_declares_capabilities() -> None:
    manifest = load_manifest(PACK)

    assert manifest.id == "task-crud-backend"
    assert manifest.kind == "capability-pack"
    assert set(manifest.provides) >= {"crud", "pagination", "search", "data_ownership"}
    assert "demo-ts-mp" in manifest.compatible_profiles
    assert any(r.target.endswith("MybatisPlusConfig.java") for r in manifest.renders)


def test_params_are_derived_from_contract() -> None:
    params = build_params(_contract())

    assert params["entity_name"] == "Task"
    assert params["table_name"] == "task"
    assert params["plural"] == "tasks"
    assert params["route_path"] == "/api/tasks"
    assert params["owner_pascal"] == "OwnerId"
    assert params["search_pascal"] == "Title"


def test_rendered_files_are_parameterized(tmp_path: Path) -> None:
    output = tmp_path / "gen"

    written = materialize_backend(TEMPLATE_BACKEND, PACK, _contract(), output)

    assert written
    entity = (output / "src/main/java/com/agentforge/template/entity/Task.java").read_text("utf-8")
    assert "class Task" in entity and '@TableName("task")' in entity
    controller = (
        output / "src/main/java/com/agentforge/template/controller/TaskController.java"
    ).read_text("utf-8")
    assert '@RequestMapping("/api/tasks")' in controller
    service = (
        output / "src/main/java/com/agentforge/template/service/TaskService.java"
    ).read_text("utf-8")
    assert "requireOwned" in service and "ErrorCode.FORBIDDEN" in service
    migration = (
        output / "src/main/resources/db/migration/V2__create_task.sql"
    ).read_text("utf-8")
    assert "CREATE TABLE task" in migration and "owner_id BIGINT NOT NULL" in migration
    assert "mybatis-plus-jsqlparser" in (output / "pom.xml").read_text("utf-8")


def test_render_is_deterministic(tmp_path: Path) -> None:
    contract = _contract()

    first = materialize_backend(TEMPLATE_BACKEND, PACK, contract, tmp_path / "a")
    second = materialize_backend(TEMPLATE_BACKEND, PACK, contract, tmp_path / "b")

    assert first and second
    for path in first:
        relative = Path(path).relative_to(tmp_path / "a")
        assert Path(path).read_text("utf-8") == (tmp_path / "b" / relative).read_text("utf-8")


def test_render_rejects_contract_without_owner_column() -> None:
    entity = {
        "name": "Task",
        "source": "user_confirmed",
        "fields": [
            {"name": "id", "type": "long", "required": True},
            {"name": "title", "type": "string", "required": True},
        ],
    }

    with pytest.raises(PackRenderError) as error:
        build_params(_contract(_spec(entities=[entity])))

    assert "owner_id" in error.value.message


def test_render_rejects_unsupported_column_type() -> None:
    raw = _contract().model_dump()
    raw["tables"][0]["columns"][1]["sql_type"] = "UUID"

    with pytest.raises(PackRenderError) as error:
        build_params(BusinessContract.model_validate(raw))

    assert error.value.code == "CONTRACT_UNSUPPORTED"
