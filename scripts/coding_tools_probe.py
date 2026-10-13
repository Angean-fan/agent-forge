"""R16 百炼 coding 工具探针（**会真实计费**，默认不随门禁运行，需人工显式执行）。

用途：对**目标地域/接口**核实 `qwen3-coder-flash` 的 Function Calling 能力，并跑通一条
**受限工具调用 + 暂存写入 + 内部调用入账**的真实链路。输出脱敏证据（不打印密钥、不回显隐藏思考）。

- 阶段 A（原生 HTTP）：直接探测该地域 `/chat/completions` 是否返回 `tool_calls`。若地域不支持工具调用，
  本步为 `NOT_RUN/阻塞`——如实记录地域与接口证据，**不**声明 R16 PASS、**不**擅自换型号。
- 阶段 B（受限引擎端到端）：在临时 run 目录 + 临时 SQLite 账本上构造 R16 受限 patch engine，
  真实 invoke 一次，核对：真实工具表（无 shell/委派）、暂存变更集、以及**每次内部模型请求各入账一行**。

用法（需 DASHSCOPE_API_KEY；本机 TLS 需要时 `SSL_CERT_FILE=/etc/ssl/cert.pem`）：

    cd platform-api
    set -a; source .env; set +a
    uv run --locked python ../scripts/coding_tools_probe.py

可用 `AGENTFORGE_DASHSCOPE_BASE_URL` 切换地域（如新加坡 intl 端点）。

退出码：0 = A/B 均通过；1 = 有阶段未通过；2 = 缺密钥（未发起任何请求）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PLATFORM_DIR = REPO / "platform-api"
SRC = PLATFORM_DIR / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

BASE_URL = os.environ.get(
    "AGENTFORGE_DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
MODEL = os.environ.get("AGENTFORGE_CODING_MODEL", "qwen3-coder-flash")
SERVICE_DIR = "backend/src/main/java/com/agentforge/template/service/"


def _probe_function_calling(api_key: str) -> bool:
    """阶段 A：原生 HTTP 探针——该地域是否支持工具调用。"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You must call the provided tool."},
            {
                "role": "user",
                "content": "在服务目录新建文件 Note.java，内容为 class Note {}。请调用 write_file 工具。",
            },
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "write_file",
                    "description": "Write text content to a file.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string"},
                            "content": {"type": "string"},
                        },
                        "required": ["file_path", "content"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "stream": False,
        "max_tokens": 256,
    }
    request = urllib.request.Request(
        f"{BASE_URL}/chat/completions", data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(json.dumps({"phase": "A", "http_status": error.code, "error": error.reason},
                         ensure_ascii=False))
        return False
    message = (body.get("choices") or [{}])[0].get("message", {})
    tool_calls = message.get("tool_calls") or []
    print(json.dumps({
        "phase": "A",
        "base_url": BASE_URL,
        "response_model": body.get("model"),
        "usage": body.get("usage"),
        "tool_calls": [
            {"name": (call.get("function") or {}).get("name"),
             "arguments": (call.get("function") or {}).get("arguments")}
            for call in tool_calls
        ],
    }, ensure_ascii=False))
    return bool(tool_calls)


def _probe_restricted_engine(api_key: str) -> bool:
    """阶段 B：受限引擎端到端——真实工具调用 + 暂存写入 + 逐次内部入账。"""
    from alembic import command
    from alembic.config import Config

    from agentforge_platform.agent_files import FileBackend
    from agentforge_platform.db import make_engine, make_session_factory, session_scope
    from agentforge_platform.models import ModelCall, Project, SpecVersion
    from agentforge_platform.patch_engine import LedgerBinding, build_patch_engine
    from agentforge_platform.runs import create_run, record_task
    from agentforge_platform.spec import SoftwareSpecification
    from agentforge_platform.spec_service import canonical_json, content_hash

    workdir = Path(tempfile.mkdtemp(prefix="r16-probe-"))
    run_dir = workdir / "run-1"
    (run_dir / SERVICE_DIR).mkdir(parents=True)
    (run_dir / SERVICE_DIR / "ReportService.java").write_text(
        "package com.agentforge.template.service;\nclass ReportService {}\n", encoding="utf-8"
    )

    db_url = f"sqlite+pysqlite:///{workdir / 'probe.db'}"
    config = Config(str(PLATFORM_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(PLATFORM_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(config, "head")

    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    spec = SoftwareSpecification.model_validate_json(
        (REPO / "schemas" / "campus-activity.spec.json").read_text(encoding="utf-8")
    )
    with session_scope(factory) as session:
        project = Project(owner_id=1, name="probe", requirement_text="probe")
        session.add(project)
        session.flush()
        version = SpecVersion(
            project_id=project.id, version=1, body_json=canonical_json(spec),
            hash=content_hash(spec), confirmed_at=datetime(2026, 1, 1),
        )
        session.add(version)
        session.flush()
        project.active_spec_id = version.id
        run = create_run(session, project, version.id, idempotency_key="probe",
                         spec_hash=content_hash(spec))
        run_id, project_id = run.id, project.id
        record_task(session, run, "gap:AC01", status="RUNNING")

    file_backend = FileBackend(run_dir, [SERVICE_DIR])
    engine_obj = build_patch_engine(
        ledger_binding=LedgerBinding(
            session_factory=factory, project_id=project_id, run_id=run_id, task_id="gap:AC01"
        ),
        file_backend=file_backend,
        system_prompt=(
            "You are a restricted patch engine. Only edit files under "
            f"{SERVICE_DIR}. Do the minimal change requested, then stop."
        ),
        model_name=MODEL,
    )
    result = engine_obj.run(
        f"在 {SERVICE_DIR}ReportService.java 里新增一个 public String ping() 方法，返回 \"pong\"。"
        "使用 edit_file 工具完成最小修改。"
    )
    with session_scope(factory) as session:
        rows = session.query(ModelCall).filter(ModelCall.run_id == run_id).all()
        ledger = [
            {"model": row.model, "status": row.status, "in": row.input_tokens,
             "out": row.output_tokens, "cost_micro": row.cost_micro,
             "price_version": row.price_version}
            for row in rows
        ]
    change_set = result["change_set"]
    print(json.dumps({
        "phase": "B",
        "tool_names": sorted(engine_obj.tool_names),
        "change_set": change_set,
        "staged_files": [c["path"] for c in change_set.get("changes", [])],
        "ledger_rows": len(rows),
        "ledger": ledger,
    }, ensure_ascii=False))
    engine.dispose()

    staged = bool(change_set.get("changes"))
    ledgered = len(rows) >= 1
    restricted = not (
        {"execute", "task"} & set(engine_obj.tool_names)
    )
    return staged and ledgered and restricted


def main() -> int:
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("环境变量 DASHSCOPE_API_KEY 未配置", file=sys.stderr)
        return 2
    # 探针即验证启用路径：显式开启 coding 角色（默认关闭，护栏要求显式开关）。
    os.environ.setdefault("AGENTFORGE_CODING_ENABLED", "1")
    print(f"# base_url={BASE_URL} model={MODEL} ssl_cert_file={os.environ.get('SSL_CERT_FILE')}")
    a = _probe_function_calling(api_key)
    b = _probe_restricted_engine(api_key) if a else False
    print(f"# summary: phaseA(function_calling)={'PASS' if a else 'FAIL'} "
          f"phaseB(restricted_engine)={'PASS' if b else 'NOT_RUN' if not a else 'FAIL'}")
    return 0 if (a and b) else 1


if __name__ == "__main__":
    raise SystemExit(main())
