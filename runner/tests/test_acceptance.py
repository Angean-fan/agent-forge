"""M18 验收辅助函数单元测试（镜像提取/对账、契约角色判定；不依赖 Docker）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_runner.acceptance import (
    _contract_limited_role,
    _role_allowed,
    compose_mysql_image,
    lock_mysql_image,
)

DIGEST = "sha256:" + "a" * 64


def _write_artifact(root: Path, *, limited_role: bool = False) -> None:
    (root / "compose.yaml").write_text(
        f"services:\n  mysql:\n    image: mysql:8.0.43@{DIGEST}\n", encoding="utf-8"
    )
    (root / "asset-lock.json").write_text(
        json.dumps({"mysql_image": {"reference": "mysql:8.0.43", "digest": DIGEST}}),
        encoding="utf-8",
    )
    roles = [
        {"role": "ADMIN", "actions": ["create", "read", "update"]},
        {"role": "MEMBER", "actions": ["read"] if limited_role else ["create", "read", "update"]},
    ]
    routes = [
        {"operation": "create", "allowed_roles": ["ADMIN"] if limited_role else ["ADMIN", "MEMBER"]},
        {"operation": "delete", "allowed_roles": []},
    ]
    (root / "contract.json").write_text(
        json.dumps({"permissions": [{"entity": "Task", "roles": roles}], "routes": routes}),
        encoding="utf-8",
    )


def test_compose_mysql_image(tmp_path: Path) -> None:
    _write_artifact(tmp_path)
    assert compose_mysql_image(tmp_path) == ("mysql:8.0.43", DIGEST)


def test_lock_mysql_image_and_reconcile(tmp_path: Path) -> None:
    _write_artifact(tmp_path)
    assert lock_mysql_image(tmp_path) == compose_mysql_image(tmp_path)


def test_contract_limited_role(tmp_path: Path) -> None:
    _write_artifact(tmp_path, limited_role=True)
    assert _contract_limited_role(tmp_path) == "MEMBER"


def test_contract_without_limited_role(tmp_path: Path) -> None:
    _write_artifact(tmp_path, limited_role=False)
    assert _contract_limited_role(tmp_path) is None


def test_role_allowed_reflects_contract(tmp_path: Path) -> None:
    _write_artifact(tmp_path, limited_role=True)
    assert _role_allowed(tmp_path, "create", "MEMBER") is False
    assert _role_allowed(tmp_path, "create", "ADMIN") is True
    assert _role_allowed(tmp_path, "delete", "ADMIN") is False
