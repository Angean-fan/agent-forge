"""M12 前端 CRUD 能力包测试（加载 / 参数化 / 渲染 / 确定性）。"""

from __future__ import annotations

from pathlib import Path

from agentforge_platform.capability_packs import build_params, load_manifest, materialize
from agentforge_platform.contract import compile_from_spec
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]
PACK = REPO / "capability-packs" / "task-crud-frontend"
TEMPLATE_FRONTEND = REPO / "templates" / "frontend"
SAMPLE_SPEC = REPO / "schemas" / "task-management.spec.json"


def _contract():
    spec = SoftwareSpecification.model_validate_json(SAMPLE_SPEC.read_text("utf-8"))
    return compile_from_spec(spec)


def test_manifest_declares_frontend_capabilities() -> None:
    manifest = load_manifest(PACK)

    assert manifest.id == "task-crud-frontend"
    assert "crud-ui" in manifest.provides
    assert "demo-ts-mp" in manifest.compatible_profiles
    assert not manifest.dependencies


def test_params_expose_ts_contract() -> None:
    params = build_params(_contract())

    assert params["entity_camel"] == "task"
    assert params["api_path"] == "/tasks"
    assert params["ui_path"] == "/tasks"
    assert {f["java_name"]: f["ts_type"] for f in params["view_fields"]}["owner_id"] == "number"
    assert {f["java_name"]: f["ts_type"] for f in params["view_fields"]}["title"] == "string"


def test_rendered_frontend_files(tmp_path: Path) -> None:
    output = tmp_path / "fe"

    written = materialize(TEMPLATE_FRONTEND, PACK, _contract(), output)

    assert written
    types = (output / "src/types/task.ts").read_text("utf-8")
    assert "interface TaskView" in types and "description: string | null" in types
    assert "interface TaskInput" in types and "description?: string" in types

    api = (output / "src/api/task.ts").read_text("utf-8")
    assert "listTasks" in api and "/tasks?" in api and "method: 'POST'" in api

    view = (output / "src/views/TaskListView.vue").read_text("utf-8")
    assert "el-table" in view and "listTasks" in view and "ElMessage.error" in view

    router_module = (output / "src/router/modules/task.ts").read_text("utf-8")
    assert "path: '/tasks'" in router_module and "TaskListView" in router_module

    assert (output / "test/task-api.spec.ts").exists()


def test_render_is_deterministic(tmp_path: Path) -> None:
    contract = _contract()

    first = materialize(TEMPLATE_FRONTEND, PACK, contract, tmp_path / "a")
    second = materialize(TEMPLATE_FRONTEND, PACK, contract, tmp_path / "b")

    assert first and second
    for path in first:
        relative = Path(path).relative_to(tmp_path / "a")
        assert Path(path).read_text("utf-8") == (tmp_path / "b" / relative).read_text("utf-8")
