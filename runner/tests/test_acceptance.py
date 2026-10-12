"""M18 验收辅助函数单元测试（镜像提取/对账、契约角色判定；不依赖 Docker）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_runner.acceptance import (
    _contract_limited_role,
    _is_campus_project,
    _role_allowed,
    _run_campus_cases,
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


class _FakeBackend:
    """校园活动形态的假后端：驱动 _is_campus_project / _run_campus_cases。"""

    def __init__(self) -> None:
        self.activities: list[dict] = []
        self.enrollments: list[dict] = []

    def login(self, base, username, password):
        return {"admin": "admin-token", "member": "member-token"}.get(username if password.endswith("123") else "")

    def http(self, method, url, token=None, body=None):
        path = url.split("/api", 1)[1] if "/api" in url else url
        role = "ADMIN" if token == "admin-token" else "MEMBER"
        if method == "GET" and path.startswith("/activities"):
            return 200, {"items": self.activities, "total": len(self.activities)}
        if method == "POST" and path == "/activities":
            if role != "ADMIN":
                return 403, None
            record = {"id": len(self.activities) + 1, **body}
            self.activities.append(record)
            return 200, record
        if method == "PUT" and path.startswith("/activities/"):
            activity_id = int(path.rsplit("/", 1)[-1])
            record = next((a for a in self.activities if a["id"] == activity_id), None)
            if record is not None and body:
                record.update(body)
            return 200, record
        if method == "POST" and path == "/enrollments":
            activity = next((a for a in self.activities if a["id"] == body["activity_id"]), None)
            if activity is not None and activity.get("status") in {"CLOSED", "ENDED"}:
                return 409, None
            duplicate = any(
                e["activity_id"] == body["activity_id"] and e["owner"] == role
                for e in self.enrollments
            )
            if duplicate:
                return 409, None
            record = {"id": len(self.enrollments) + 1, "owner": role, **body}
            self.enrollments.append(record)
            return 200, record
        if method == "GET" and path.startswith("/enrollments?"):
            return 200, {"items": self.enrollments, "total": len(self.enrollments)}
        if method == "GET" and path.startswith("/enrollments/"):
            return (403, None) if role == "ADMIN" else (200, {"id": 1})
        return 404, None


def test_is_campus_project_detects_shape(monkeypatch) -> None:
    from agentforge_runner import acceptance

    fake = _FakeBackend()
    monkeypatch.setattr(acceptance, "_login", fake.login)
    monkeypatch.setattr(acceptance, "_http", fake.http)

    assert _is_campus_project("http://x") is True


def test_run_campus_cases_all_pass_against_fake_backend(monkeypatch) -> None:
    from agentforge_runner import acceptance

    fake = _FakeBackend()
    monkeypatch.setattr(acceptance, "_login", fake.login)
    monkeypatch.setattr(acceptance, "_http", fake.http)
    cases: list[tuple[str, bool]] = []

    _run_campus_cases("http://x", lambda name, passed, detail=None: cases.append((name, passed)))

    names = [name for name, _ in cases]
    assert set(names) >= {
        "login_admin", "login_member", "campus_admin_publish", "campus_member_cannot_publish",
        "campus_enroll", "campus_no_duplicate", "campus_list_total",
        "campus_cross_user_denied", "campus_persists", "campus_closed_rejected",
    }
    assert all(passed for _, passed in cases), [c for c in cases if not c[1]]
