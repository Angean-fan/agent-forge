"""三角色模型路由冒烟（**会真实计费**，默认不随门禁运行，需人工显式执行）。

用途：在**真实数据库 + 真实账本**上，按 `AGENTFORGE_MODELS_CONFIG` 指定的路由配置，
对每个角色各做**一次**真实模型调用，核对三件事：

1. 传输与鉴权：走 `production_routing.build_client_for_role()`，即与生产完全相同的客户端路径；
2. 内容可用：返回正文非空（思考型模型空正文说明 max_output_tokens 给小了）；
3. 入账正确：`invoke()` 落 `model_call` 一行，打印 provider/model/status/reserved/cost，
   未配 `price_micro` 的路由如实显示 `UNKNOWN`（**不会**伪算成 0）。

输出脱敏：只打印环境变量名与端点，绝不打印密钥。

用法（在 platform-api 目录下，先加载 .env）：

    set -a; source .env; set +a
    uv run --locked python ../scripts/provider_routing_smoke.py --roles user_interaction,dept_coder,dept_tester

常用参数：

    --roles  a,b,c    只跑这些角色（默认三者：需求分析 / 部门开发 / 测试工程师）
    --roles all       跑配置中的全部角色（注意：无价格的每次调用默认预留 1 元）
    --tokens N        单次 max_output_tokens（默认 512）
    --config PATH     临时覆盖 AGENTFORGE_MODELS_CONFIG

退出码：0 = 全部通过；1 = 有角色失败；2 = 缺密钥（未发起任何请求）。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "platform-api" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_ROLES = ["user_interaction", "dept_coder", "dept_tester"]
PROMPT = "用一句话回答：当前模型名称与所在厂商分别是什么？只需一句中文。"


def _bootstrap_models_config(config: str | None) -> None:
    if config:
        os.environ["AGENTFORGE_MODELS_CONFIG"] = str(Path(config).resolve())


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--roles", default=",".join(DEFAULT_ROLES))
    parser.add_argument("--tokens", type=int, default=512)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)
    _bootstrap_models_config(args.config)

    # 延迟导入：确保 AGENTFORGE_MODELS_CONFIG 在读取路由前生效。
    from sqlalchemy.orm import sessionmaker

    from agentforge_platform.db import get_database_url, make_engine, session_scope
    from agentforge_platform.errors import AppException
    from agentforge_platform.model_gateway import KNOWN_ROLES, invoke
    from agentforge_platform.models import ModelCall, Project
    from agentforge_platform.production_routing import build_client_for_role, role_status

    roles = sorted(KNOWN_ROLES) if args.roles.strip() == "all" else [
        r.strip() for r in args.roles.split(",") if r.strip()
    ]
    unknown = [r for r in roles if r not in KNOWN_ROLES]
    if unknown:
        print(f"未知角色：{', '.join(unknown)}（可用：{', '.join(sorted(KNOWN_ROLES))}）")
        return 1

    missing: list[str] = []
    for role in roles:
        status = role_status(role)
        print(
            f"[route] {role:18s} provider={status.provider:16s} model={status.model:16s} "
            f"flag_on={status.flag_on} key_present={status.key_present} enabled={status.enabled} "
            f"reason={status.reason} endpoint={status.endpoint}"
        )
        if not status.key_present:
            missing.append(f"{role}:{status.provider}")
    if missing:
        print(f"缺密钥：{', '.join(missing)}（未发起任何请求）")
        return 2

    url = get_database_url()
    print(f"[db] {url.split('@')[-1] if '@' in url else url}")
    engine = make_engine(url)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    project_name = "routing-smoke"
    with session_scope(factory) as session:
        project = Project(owner_id=1, name=project_name, requirement_text="模型路由冒烟")
        session.add(project)
        session.flush()
        project_id = project.id
    print(f"[project] id={project_id} name={project_name}")

    failures = 0
    for role in roles:
        try:
            client = build_client_for_role(role)
            with session_scope(factory) as session:
                result = invoke(
                    session,
                    call_id=f"smoke-{role}-{int(time.time() * 1000)}",
                    model_role=role,
                    client=client,
                    prompt=PROMPT,
                    project_id=project_id,
                    max_input_tokens=2000,
                    max_output_tokens=args.tokens,
                )
            text = (result.content or "").strip().replace("\n", " ")
            usage = result.usage
            ok = bool(text)
            print(
                f"[call] {role:18s} {'PASS' if ok else 'EMPTY'} chars={len(text)} "
                f"in={usage.input_tokens if usage else '?'} out={usage.output_tokens if usage else '?'} "
                f"msg={text[:60]!r}"
            )
            failures += 0 if ok else 1
        except AppException as exc:
            print(f"[call] {role:18s} FAIL code={exc.code} msg={exc.message}")
            failures += 1
        except Exception as exc:  # noqa: BLE001 - 冒烟脚本需要如实打印任何失败
            print(f"[call] {role:18s} FAIL {type(exc).__name__}: {exc}")
            failures += 1

    with session_scope(factory) as session:
        rows = (
            session.query(ModelCall)
            .filter(ModelCall.project_id == project_id)
            .order_by(ModelCall.call_id)
            .all()
        )
        for row in rows:
            print(
                f"[ledger] {row.model_role:18s} provider={row.provider:16s} model={row.model:16s} "
                f"status={row.status:8s} reserved={row.reserved_micro} cost={row.cost_micro} "
                f"price_version={row.price_version}"
            )

    unsettled = [row for row in rows if row.status != "SETTLED"]
    print(f"[result] roles={len(roles)} call_failures={failures} unsettled_rows={len(unsettled)}")
    if unsettled:
        print(
            "[warn] 存在未 SETTLED 的账本行（多为路由未配 price_micro 的 UNKNOWN：金额如实记为 NULL，"
            "预留继续占用项目预算）。这不代表调用失败，调用成败以上面的 [call] 行为准。"
        )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
