"""M18 验收辅助函数单元测试（镜像提取/对账、契约角色判定；不依赖 Docker）。"""

from __future__ import annotations

import json
from pathlib import Path

from agentforge_runner.acceptance import (
    _contract_limited_role,
    _is_campus_project,
    _is_factory_openapi_project,
    _is_lending_contract,
    _role_allowed,
    _run_campus_cases,
    _run_factory_openapi_cases,
    _run_lending_cases,
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


def test_lending_contract_shape_is_explicit(tmp_path: Path) -> None:
    (tmp_path / "contract.json").write_text(
        json.dumps({"tables": [{"table": "book"}, {"table": "loan"}]}), encoding="utf-8"
    )
    assert _is_lending_contract(tmp_path) is True
    (tmp_path / "contract.json").write_text(
        json.dumps({"tables": [{"table": "task"}]}), encoding="utf-8"
    )
    assert _is_lending_contract(tmp_path) is False


def test_lending_fixed_cases_reject_duplicate_and_check_return(monkeypatch) -> None:
    from agentforge_runner import acceptance

    books: dict[int, dict] = {}
    loans: dict[int, dict] = {}

    def login(base, username, password):
        return {"admin": "admin-token", "member": "member-token"}.get(username)

    def http(method, url, token=None, body=None):
        path = url.split("/api", 1)[1]
        admin = token == "admin-token"
        if method == "POST" and path == "/books":
            if not admin:
                return 403, None
            book = {"id": len(books) + 1, **body}
            books[book["id"]] = book
            return 200, book
        if method == "GET" and path.startswith("/books/"):
            return 200, books[int(path.rsplit("/", 1)[-1])]
        if method == "GET" and path == "/books":
            return 200, {"total": len(books)}
        if method in {"PUT", "DELETE"} and path.startswith("/books/"):
            return 409, None
        if method == "POST" and path == "/loans":
            book = books[body["book_id"]]
            if book["status"] != "AVAILABLE":
                return 409, None
            book["status"] = "BORROWED"
            loan = {"id": len(loans) + 1, "owner": token, **body}
            loans[loan["id"]] = loan
            return 200, loan
        if method == "GET" and path.startswith("/loans/"):
            loan = loans[int(path.rsplit("/", 1)[-1])]
            return (200, loan) if loan["owner"] == token else (403, None)
        if method == "GET" and path.startswith("/loans?"):
            if "size=100000" in path:
                return 400, None
            return 200, {"total": sum(loan["owner"] == token for loan in loans.values())}
        if method == "PUT" and path.startswith("/loans/"):
            loan = loans[int(path.rsplit("/", 1)[-1])]
            if loan["owner"] != token:
                return 403, None
            loan["status"] = "RETURNED"
            books[loan["book_id"]]["status"] = "AVAILABLE"
            return 200, loan
        return 404, None

    monkeypatch.setattr(acceptance, "_login", login)
    monkeypatch.setattr(acceptance, "_http", http)
    cases: list[tuple[str, bool]] = []
    _run_lending_cases("http://x", lambda name, passed, detail=None: cases.append((name, passed)))

    assert cases and not [name for name, passed in cases if not passed]
    assert {"lending_borrow", "lending_duplicate_rejected", "lending_return_releases_book"} <= {
        name for name, _ in cases
    }


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


def test_is_factory_openapi_project_detects_factory_markers(tmp_path: Path) -> None:
    assert _is_factory_openapi_project(tmp_path) is False
    oa = tmp_path / "backend" / "openapi.json"
    oa.parent.mkdir(parents=True)
    oa.write_text('{"openapi": "3.0.3", "paths": {}}', encoding="utf-8")
    assert _is_factory_openapi_project(tmp_path) is True


def test_run_factory_openapi_cases_passes_for_ecommerce_sandbox(tmp_path: Path, monkeypatch) -> None:
    from agentforge_runner import acceptance

    oa = tmp_path / "backend" / "openapi.json"
    oa.parent.mkdir(parents=True)
    oa.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/api/products": {"get": {"summary": "商品列表"}},
                    "/api/orders": {
                        "get": {"summary": "订单列表", "security": [{"bearerAuth": []}]},
                    },
                    "/api/payments/sandbox-pay": {
                        "post": {"summary": "本地沙箱支付", "security": [{"bearerAuth": []}]},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_login(base, username, password):
        return {"admin": "admin-tok", "member": "member-tok"}.get(username)

    def fake_http(method, url, token=None, body=None):
        path = url.split("http://x", 1)[1]
        if method == "GET" and path == "/api/health":
            return 200, {"status": "UP"}
        if method == "GET" and path == "/api/products":
            return 200, {"items": []}
        if method == "GET" and path == "/api/orders":
            return (200, {"items": []}) if token else (401, {"error": "unauthorized"})
        if method == "POST" and path == "/api/payments/sandbox-pay":
            return (400, {"error": "orderId required"}) if token else (401, None)
        return 404, None

    monkeypatch.setattr(acceptance, "_login", fake_login)
    monkeypatch.setattr(acceptance, "_http", fake_http)
    cases: list[tuple[str, bool, str | None]] = []
    _run_factory_openapi_cases(
        "http://x",
        tmp_path,
        lambda name, passed, detail=None: cases.append((name, passed, detail)),
    )

    names = {name for name, _, _ in cases}
    assert {
        "factory_health",
        "factory_openapi_valid",
        "login_admin",
        "login_member",
        "factory_get_api_products",
        "factory_auth_guard_api_orders",
        "factory_get_api_orders",
        "factory_sandbox_payment_routed",
    } <= names
    assert all(passed for _, passed, _ in cases), [c for c in cases if not c[1]]


def test_run_factory_openapi_cases_flags_missing_auth_guard_or_500(tmp_path: Path, monkeypatch) -> None:
    from agentforge_runner import acceptance

    oa = tmp_path / "backend" / "openapi.json"
    oa.parent.mkdir(parents=True)
    oa.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/api/orders": {
                        "get": {"summary": "订单列表", "security": [{"bearerAuth": []}]},
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(acceptance, "_login", lambda *a: "tok")

    def broken_http(method, url, token=None, body=None):
        if url.endswith("/api/health"):
            return 200, {"status": "UP"}
        # 鉴权泄露（无 token 返 200）且带 token 报 500
        return (500, None) if token else (200, {"leaked": True})

    monkeypatch.setattr(acceptance, "_http", broken_http)
    results: dict[str, bool] = {}
    _run_factory_openapi_cases(
        "http://x",
        tmp_path,
        lambda name, passed, detail=None: results.__setitem__(name, passed),
    )
    assert results["factory_auth_guard_api_orders"] is False
    assert results["factory_get_api_orders"] is False


def test_factory_openapi_takes_precedence_over_campus_and_respects_x_auth(
    tmp_path: Path, monkeypatch
) -> None:
    """含 /api/activities 的工厂项目（如社团管理系统）必须走工厂 OpenAPI 验收而非旧校园 Demo，并识别 x-auth。"""
    from agentforge_runner import acceptance

    assert acceptance._op_requires_auth({"security": [{"bearerAuth": []}]}) is True
    assert acceptance._op_requires_auth({"security": []}) is False
    assert acceptance._op_requires_auth({"x-auth": ["USER"]}) is True
    assert acceptance._op_requires_auth({"x-auth": ["PUBLIC"]}) is False
    assert acceptance._op_requires_auth({}) is False

    oa = tmp_path / "backend" / "openapi.json"
    oa.parent.mkdir(parents=True)
    oa.write_text(
        json.dumps(
            {
                "openapi": "3.0.3",
                "paths": {
                    "/api/activities": {
                        "get": {"summary": "社团活动列表", "x-auth": ["USER"]},
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(acceptance, "_login", lambda *a: "tok")

    def fake_http(method, url, token=None, body=None):
        if url.endswith("/api/health"):
            return 200, {"status": "UP"}
        if url.endswith("/api/activities"):
            return (200, {"items": []}) if token else (401, None)
        return 404, None

    monkeypatch.setattr(acceptance, "_http", fake_http)
    cases: dict[str, bool] = {}
    _run_factory_openapi_cases(
        "http://x",
        tmp_path,
        lambda name, passed, detail=None: cases.__setitem__(name, passed),
    )
    assert cases["factory_auth_guard_api_activities"] is True
    assert cases["factory_get_api_activities"] is True
    assert "campus_admin_publish" not in cases

    # 验证 run_acceptance 源码中 _is_factory_openapi_project 优先于 _is_campus_project
    src = Path(acceptance.__file__).read_text(encoding="utf-8")
    assert src.index("elif _is_factory_openapi_project") < src.index("elif _is_campus_project")
