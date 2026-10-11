"""M02 契约测试：/health、request_id 传播与统一成功/失败响应结构。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentforge_platform.errors import AppException, ErrorCode
from agentforge_platform.main import create_app

client = TestClient(create_app())


def test_health_returns_parseable_success_envelope() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "ok"
    assert body["data"]["version"]
    assert body["request_id"]


def test_health_response_carries_matching_request_id_header() -> None:
    response = client.get("/health")

    body = response.json()
    assert response.headers["X-Request-ID"] == body["request_id"]


def test_client_supplied_request_id_is_echoed() -> None:
    response = client.get("/health", headers={"X-Request-ID": "trace-abc-123"})

    assert response.status_code == 200
    assert response.json()["request_id"] == "trace-abc-123"
    assert response.headers["X-Request-ID"] == "trace-abc-123"


def test_error_envelope_carries_request_id_and_error_fields() -> None:
    response = client.get("/no-such-path")

    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert body["error"]["message"]
    assert body["error"]["retryable"] is False
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_domain_error_is_mapped_to_envelope() -> None:
    app = create_app()

    @app.get("/boom")
    def boom() -> None:
        raise AppException(ErrorCode.BAD_REQUEST, "boom", status_code=409, retryable=True)

    response = TestClient(app, raise_server_exceptions=False).get("/boom")

    assert response.status_code == 409
    body = response.json()
    assert body["error"] == {"code": "BAD_REQUEST", "message": "boom", "retryable": True}
    assert body["request_id"]


def test_validation_error_is_mapped_to_envelope() -> None:
    app = create_app()

    @app.get("/needs-int")
    def needs_int(q: int) -> dict[str, int]:
        return {"q": q}

    response = TestClient(app).get("/needs-int")

    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert body["request_id"]


def test_unexpected_error_does_not_leak_details() -> None:
    app = create_app()

    @app.get("/kaboom")
    def kaboom() -> None:
        raise RuntimeError("secret_value_should_not_leak")

    response = TestClient(app, raise_server_exceptions=False).get("/kaboom")

    assert response.status_code == 500
    body = response.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert body["error"]["message"] == "Internal server error."
    assert "secret_value_should_not_leak" not in response.text
