"""M18 全新 MySQL 初始化 + 启动 + 固定 HTTP 验收。

在每次 run 的隔离网络内：校验 MySQL 镜像 digest（与模板 compose、资产锁对账）→ 构建后端 jar →
启动 MySQL 与后端（Flyway 迁移）→ 播种演示用户 → 执行**平台固定**的 HTTP 用例
（登录/CRUD/分页/搜索/归属/角色/分页上限）→ 清理。断言不接受生成代码修改。
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from agentforge_runner.config import RunnerConfig
from agentforge_runner.container import build_run_argv, container_runtime_available
from agentforge_runner.tasks import get_task

_STRICT = ConfigDict(extra="forbid")
_ADMIN = ("admin", "admin123")
_MEMBER = ("member", "member123")
_ADMIN_ROLE = "ADMIN"
_DB_NAME = "agentforge"
_DB_USER = "agentforge"
_DB_PASSWORD = "agentforge_pw"
_ROOT_PASSWORD = "root_pw"
_JWT_SECRET = "acceptance-secret-acceptance-secret-1234"


class CaseResult(BaseModel):
    model_config = _STRICT

    name: str
    status: str
    detail: str | None = None


class AcceptanceResult(BaseModel):
    model_config = _STRICT

    artifact_dir: str
    mysql_image: str
    mysql_digest: str
    digest_reconciled: bool
    overall: str
    cases: list[CaseResult]


def _sh(argv: list[str], timeout: int = 900) -> tuple[int, str]:
    try:
        completed = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    return completed.returncode, (completed.stdout + b"\n" + completed.stderr).decode("utf-8", "replace")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def compose_mysql_image(artifact_dir: Path) -> tuple[str, str] | None:
    compose = artifact_dir / "compose.yaml"
    if not compose.is_file():
        return None
    match = re.search(
        r"image:\s*(mysql:[0-9][^\s@]*)@(sha256:[0-9a-f]{64})", compose.read_text("utf-8")
    )
    return (match.group(1), match.group(2)) if match else None


def lock_mysql_image(artifact_dir: Path) -> tuple[str, str] | None:
    lock_path = artifact_dir / "asset-lock.json"
    if not lock_path.is_file():
        return None
    image = json.loads(lock_path.read_text("utf-8")).get("mysql_image") or {}
    if image.get("reference") and image.get("digest"):
        return (image["reference"], image["digest"])
    return None


def _read_contract(artifact_dir: Path) -> dict | None:
    contract_path = artifact_dir / "contract.json"
    if not contract_path.is_file():
        return None
    return json.loads(contract_path.read_text("utf-8"))


def _contract_limited_role(artifact_dir: Path) -> str | None:
    contract = _read_contract(artifact_dir)
    if contract is None:
        return None
    for matrix in contract.get("permissions", []):
        for role in matrix.get("roles", []):
            if "create" not in role.get("actions", []):
                return role["role"]
    return None


def _role_allowed(artifact_dir: Path, operation: str, role: str) -> bool:
    contract = _read_contract(artifact_dir)
    if contract is None:
        return False
    for route in contract.get("routes", []):
        if route.get("operation") == operation:
            return role in route.get("allowed_roles", [])
    return False


def _http(method: str, url: str, token: str | None = None, body: dict | None = None) -> tuple[int, object]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8")
        return error.code, (json.loads(raw) if raw else None)
    except (urllib.error.URLError, http.client.HTTPException, OSError):
        return 0, None


def _login(base: str, username: str, password: str) -> str | None:
    status, body = _http("POST", f"{base}/api/auth/login", body={"username": username, "password": password})
    if status == 200 and isinstance(body, dict):
        return body.get("token")
    return None


def _bcrypt_hash(password: str) -> str:
    import bcrypt

    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()


def _wait(predicate, timeout: float, interval: float = 1.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def run_acceptance(artifact_dir: Path, config: RunnerConfig) -> AcceptanceResult:
    artifact_dir = artifact_dir.resolve()
    compose_image = compose_mysql_image(artifact_dir)
    lock_image = lock_mysql_image(artifact_dir)
    digest_reconciled = compose_image is not None and compose_image == lock_image
    reference = compose_image[0] if compose_image else ""
    digest = compose_image[1] if compose_image else ""

    cases: list[CaseResult] = []
    run_id = uuid.uuid4().hex[:8]
    network = f"af-net-{run_id}"
    mysql_name = f"af-mysql-{run_id}"
    backend_name = f"af-be-{run_id}"
    base: dict[str, str] = {}

    def teardown() -> None:
        _sh(["docker", "rm", "-f", backend_name, mysql_name], timeout=120)
        _sh(["docker", "network", "rm", network], timeout=60)

    def case(name: str, passed: bool, detail: str | None = None) -> None:
        cases.append(CaseResult(name=name, status="PASS" if passed else "FAIL", detail=detail))

    if not container_runtime_available():
        return _result(artifact_dir, reference, digest, digest_reconciled, [
            CaseResult(name="docker", status="NOT_RUN", detail="受控执行环境不可用")
        ])
    if compose_image is None:
        return _result(artifact_dir, reference, digest, digest_reconciled, [
            CaseResult(name="mysql_image", status="FAIL", detail="compose 未锁定 MySQL digest")
        ])

    try:
        # 1) 构建后端 jar（依赖经 config.container_network）
        task = get_task("backend_build")
        build_argv = build_run_argv(task, artifact_dir, config, f"af-build-{run_id}")
        code, log = _sh(build_argv, timeout=900)
        case("build_backend", code == 0, None if code == 0 else log[-500:])
        jar = artifact_dir / "backend/target/backend-0.1.0.jar"
        if code != 0 or not jar.is_file():
            return _result(artifact_dir, reference, digest, digest_reconciled, cases)
        case("static_jar", True)

        # 2) 隔离网络 + MySQL（锁 digest）
        _sh(["docker", "network", "create", network])
        _sh([
            "docker", "run", "-d", "--name", mysql_name, "--network", network,
            "--tmpfs", "/var/lib/mysql:rw,size=1g",
            "-e", f"MYSQL_ROOT_PASSWORD={_ROOT_PASSWORD}",
            "-e", f"MYSQL_DATABASE={_DB_NAME}",
            "-e", f"MYSQL_USER={_DB_USER}",
            "-e", f"MYSQL_PASSWORD={_DB_PASSWORD}",
            f"{reference}@{digest}",
        ])
        _, inspect = _sh([
            "docker", "image", "inspect", "-f", "{{index .RepoDigests 0}}", f"{reference}@{digest}"
        ])
        case("mysql_digest", digest in inspect, inspect.strip()[:120])
        mysql_up = _wait(
            lambda: _sh([
                "docker", "exec", mysql_name, "mysql", "-h", "127.0.0.1",
                f"-u{_DB_USER}", f"-p{_DB_PASSWORD}", _DB_NAME, "-e", "SELECT 1",
            ])[0] == 0,
            timeout=120,
        )
        case("mysql_ready", mysql_up)
        if not mysql_up:
            return _result(artifact_dir, reference, digest, digest_reconciled, cases)

        # 3) 启动后端（隔离参数 + 仅放行 8080 到宿主回环）
        host_port = _free_port()
        db_url = (
            f"jdbc:mysql://{mysql_name}:3306/{_DB_NAME}"
            "?useSSL=false&allowPublicKeyRetrieval=true&serverTimezone=UTC"
        )
        backend_argv = [
            "docker", "run", "-d", "--name", backend_name, "--network", network,
            "--read-only", "--tmpfs", "/tmp:rw,size=256m",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "-p", f"127.0.0.1:{host_port}:8080",
            "-v", f"{artifact_dir}:/workspace",
            "-e", f"DB_URL={db_url}", "-e", f"DB_USERNAME={_DB_USER}", "-e", f"DB_PASSWORD={_DB_PASSWORD}",
            "-e", f"JWT_SECRET={_JWT_SECRET}",
            "-e", f"APP_ADMIN_USERNAME={_ADMIN[0]}", "-e", f"APP_ADMIN_PASSWORD={_ADMIN[1]}",
            config.executor_image,
            "java", "-jar", "/workspace/backend/target/backend-0.1.0.jar",
        ]
        _sh(backend_argv)
        base["url"] = f"http://127.0.0.1:{host_port}"
        healthy = _wait(lambda: _http("GET", f"{base['url']}/api/health")[0] == 200, timeout=120)
        case("backend_startup", healthy)
        if not healthy:
            _, logs = _sh(["docker", "logs", backend_name])
            cases.append(CaseResult(name="backend_log", status="FAIL", detail=logs[-500:]))
            return _result(artifact_dir, reference, digest, digest_reconciled, cases)

        # 4) 播种演示 MEMBER 用户（隔离数据库演示数据）
        member_hash = _bcrypt_hash(_MEMBER[1])
        seed = (
            f"INSERT INTO app_user (username, password_hash, role, enabled) "
            f"VALUES ('{_MEMBER[0]}', '{member_hash}', 'MEMBER', 1)"
        )
        _sh(["docker", "exec", mysql_name, "mysql", f"-u{_DB_USER}", f"-p{_DB_PASSWORD}", _DB_NAME, "-e", seed])

        # R12 / FP06：按生成项目形态分流——优先识别含工厂权威 openapi.json / factory-blueprint.json 的
        # 工厂流水线产物（避免含 /api/activities 的多领域工厂项目被误判为旧校园活动 Demo），
        # 其次识别借阅契约与校园活动主线，否则跑原 Task 场景。
        if _is_lending_contract(artifact_dir):
            _run_lending_cases(base["url"], case)
        elif _is_factory_openapi_project(artifact_dir):
            _run_factory_openapi_cases(base["url"], artifact_dir, case)
        elif _is_campus_project(base["url"]):
            _run_campus_cases(base["url"], case)
        else:
            _run_http_cases(base["url"], artifact_dir, case)
    finally:
        teardown()

    return _result(artifact_dir, reference, digest, digest_reconciled, cases)


def _run_http_cases(base: str, artifact_dir: Path, case) -> None:
    admin_token = _login(base, *_ADMIN)
    case("login_admin", admin_token is not None)
    member_token = _login(base, *_MEMBER)
    case("login_member", member_token is not None)
    if not admin_token or not member_token:
        return

    status, created = _http(
        "POST", f"{base}/api/tasks", admin_token,
        {"title": "acceptance-task", "description": "d", "status": "OPEN"},
    )
    task_id = created.get("id") if isinstance(created, dict) else None
    case("create_task", status == 200 and task_id is not None)
    if task_id is None:
        return

    status, listing = _http("GET", f"{base}/api/tasks", admin_token)
    total = listing.get("total") if isinstance(listing, dict) else None
    case("list_total", status == 200 and total == 1, f"total={total}")

    case("get_own", _http("GET", f"{base}/api/tasks/{task_id}", admin_token)[0] == 200)

    status, _ = _http(
        "PUT", f"{base}/api/tasks/{task_id}", admin_token,
        {"title": "updated", "description": "d", "status": "DONE"},
    )
    case("update_own", status == 200)

    status, searched = _http("GET", f"{base}/api/tasks?keyword=updated", admin_token)
    hit = searched.get("total") if isinstance(searched, dict) else None
    case("search", status == 200 and hit == 1, f"total={hit}")

    case("pagination_page_zero", _http("GET", f"{base}/api/tasks?page=0", admin_token)[0] == 400)
    case("pagination_size_too_large", _http("GET", f"{base}/api/tasks?page=1&size=100000", admin_token)[0] == 400)

    case("cross_user_denied", _http("GET", f"{base}/api/tasks/{task_id}", member_token)[0] == 403)

    limited_role = _contract_limited_role(artifact_dir)
    if limited_role is not None:
        status, _ = _http(
            "POST", f"{base}/api/tasks", member_token,
            {"title": "x", "description": "d", "status": "OPEN"},
        )
        case("role_denied", status == 403, f"role={limited_role}")

    delete_expected = 200 if _role_allowed(artifact_dir, "delete", _ADMIN_ROLE) else 403
    delete_status = _http("DELETE", f"{base}/api/tasks/{task_id}", admin_token)[0]
    detail = (
        f"expected={delete_expected} got={delete_status} "
        f"dir={artifact_dir} contract={(artifact_dir / 'contract.json').is_file()}"
    )
    case("delete_own", delete_status == delete_expected, detail)


def _is_campus_project(base: str) -> bool:
    """形态检测：管理员可读 ``GET /api/activities`` → 校园活动主线；否则为原 Task 项目。"""
    token = _login(base, *_ADMIN)
    return bool(token) and _http("GET", f"{base}/api/activities", token)[0] == 200


def _is_lending_contract(artifact_dir: Path) -> bool:
    contract = _read_contract(artifact_dir)
    return bool(contract) and {table.get("table") for table in contract.get("tables", [])} == {
        "book", "loan"
    }


def _run_lending_cases(base: str, case) -> None:
    """R27 固定借阅断言：权限、借出状态、重复借阅、归还释放及归属。"""
    admin_token = _login(base, *_ADMIN)
    case("login_admin", admin_token is not None)
    member_token = _login(base, *_MEMBER)
    case("login_member", member_token is not None)
    if not admin_token or not member_token:
        return

    book_body = {"title": "r27-book", "isbn": "R27-001", "status": "AVAILABLE"}
    status, book = _http("POST", f"{base}/api/books", admin_token, book_body)
    book_id = book.get("id") if isinstance(book, dict) else None
    case("lending_admin_creates_book", status == 200 and book_id is not None)
    case("lending_member_cannot_create_book", _http(
        "POST", f"{base}/api/books", member_token, book_body,
    )[0] == 403)
    if book_id is None:
        return
    visible_status, visible_book = _http("GET", f"{base}/api/books/{book_id}", member_token)
    case("lending_member_sees_book", visible_status == 200 and isinstance(visible_book, dict)
         and visible_book.get("status") == "AVAILABLE")
    list_status, visible_books = _http("GET", f"{base}/api/books", member_token)
    case("lending_member_lists_books", list_status == 200 and isinstance(visible_books, dict)
         and visible_books.get("total") == 1)

    loan_body = {"book_id": book_id, "status": "ACTIVE"}
    status, loan = _http("POST", f"{base}/api/loans", member_token, loan_body)
    loan_id = loan.get("id") if isinstance(loan, dict) else None
    case("lending_borrow", status == 200 and loan_id is not None)
    _, borrowed_book = _http("GET", f"{base}/api/books/{book_id}", admin_token)
    case("lending_book_marked_borrowed", isinstance(borrowed_book, dict)
         and borrowed_book.get("status") == "BORROWED")
    case("lending_duplicate_rejected", _http(
        "POST", f"{base}/api/loans", member_token, loan_body,
    )[0] == 409)
    case("lending_admin_cannot_reset_borrowed_book", _http(
        "PUT", f"{base}/api/books/{book_id}", admin_token,
        {"title": "r27-book", "isbn": "R27-001", "status": "AVAILABLE"},
    )[0] == 409)
    case("lending_admin_cannot_delete_borrowed_book", _http(
        "DELETE", f"{base}/api/books/{book_id}", admin_token,
    )[0] == 409)
    if loan_id is None:
        return

    case("lending_member_reads_own", _http(
        "GET", f"{base}/api/loans/{loan_id}", member_token,
    )[0] == 200)
    case("lending_cross_user_denied", _http(
        "GET", f"{base}/api/loans/{loan_id}", admin_token,
    )[0] == 403)
    status, listed = _http("GET", f"{base}/api/loans?page=1&size=10", member_token)
    case("lending_list_total", status == 200 and isinstance(listed, dict)
         and listed.get("total") == 1)
    case("lending_pagination_limit", _http(
        "GET", f"{base}/api/loans?page=1&size=100000", member_token,
    )[0] == 400)
    returned, _ = _http(
        "PUT", f"{base}/api/loans/{loan_id}", member_token,
        {"book_id": book_id, "status": "RETURNED"},
    )
    case("lending_return", returned == 200)
    _, released = _http("GET", f"{base}/api/books/{book_id}", admin_token)
    case("lending_return_releases_book", isinstance(released, dict)
         and released.get("status") == "AVAILABLE")
    case("lending_borrow_after_return", _http(
        "POST", f"{base}/api/loans", member_token, loan_body,
    )[0] == 200)


def _run_campus_cases(base: str, case) -> None:
    """校园活动主线固定场景（R12）：管理员发布、成员报名、不可重复、越权拒绝、结束拒绝、持久化、分页总数。"""
    admin_token = _login(base, *_ADMIN)
    case("login_admin", admin_token is not None)
    member_token = _login(base, *_MEMBER)
    case("login_member", member_token is not None)
    if not admin_token or not member_token:
        return

    # 管理员发布活动
    status, activity = _http(
        "POST", f"{base}/api/activities", admin_token,
        {"title": "campus-open", "capacity": 10, "status": "OPEN"},
    )
    activity_id = activity.get("id") if isinstance(activity, dict) else None
    case("campus_admin_publish", status == 200 and activity_id is not None)
    # 成员不得发布
    case("campus_member_cannot_publish", _http(
        "POST", f"{base}/api/activities", member_token,
        {"title": "x", "capacity": 1, "status": "OPEN"},
    )[0] == 403)
    if activity_id is None:
        return

    # 成员报名 + 不可重复
    body = {"member_name": "m1", "status": "ACTIVE", "activity_id": activity_id}
    status, enrollment = _http("POST", f"{base}/api/enrollments", member_token, body)
    enrollment_id = enrollment.get("id") if isinstance(enrollment, dict) else None
    case("campus_enroll", status == 200 and enrollment_id is not None)
    case("campus_no_duplicate", _http("POST", f"{base}/api/enrollments", member_token, body)[0] == 409)

    # 分页总数（成员仅见自己的报名）
    status, listing = _http("GET", f"{base}/api/enrollments?page=1&size=10", member_token)
    total = listing.get("total") if isinstance(listing, dict) else None
    case("campus_list_total", status == 200 and total == 1, f"total={total}")

    # 越权：他人（管理员）读取该报名 → 403
    if enrollment_id is not None:
        case("campus_cross_user_denied",
             _http("GET", f"{base}/api/enrollments/{enrollment_id}", admin_token)[0] == 403)

    # 持久化：刷新后活动仍在
    case("campus_persists", _http("GET", f"{base}/api/activities/{activity_id}", admin_token)[0] == 200)

    # 活动结束后拒绝报名（独立活动，避免与唯一约束混淆）
    _, closed = _http(
        "POST", f"{base}/api/activities", admin_token,
        {"title": "campus-closed", "capacity": 1, "status": "OPEN"},
    )
    closed_id = closed.get("id") if isinstance(closed, dict) else None
    if closed_id is not None:
        _http("PUT", f"{base}/api/activities/{closed_id}", admin_token,
              {"title": "campus-closed", "capacity": 1, "status": "CLOSED"})
        case("campus_closed_rejected", _http(
            "POST", f"{base}/api/enrollments", member_token,
            {"member_name": "m2", "status": "ACTIVE", "activity_id": closed_id},
        )[0] == 409)


def _read_factory_openapi(artifact_dir: Path) -> dict | None:
    oa_path = artifact_dir / "backend" / "openapi.json"
    if not oa_path.is_file():
        return None
    try:
        parsed = json.loads(oa_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _is_factory_openapi_project(artifact_dir: Path) -> bool:
    """识别异步工厂流水线产物（含权威 ``backend/openapi.json`` 或 ``factory-blueprint.json``）。"""
    return (artifact_dir / "backend" / "openapi.json").is_file() or (
        artifact_dir / "factory-blueprint.json"
    ).is_file()


def _slug_path(path: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", path.strip("/")).strip("_").lower()
    return cleaned or "root"


def _op_requires_auth(op: dict) -> bool:
    sec = op.get("security")
    if isinstance(sec, list):
        return len(sec) > 0
    x_auth = op.get("x-auth")
    if isinstance(x_auth, list) and x_auth:
        return "PUBLIC" not in {str(item).upper() for item in x_auth}
    return False


def _run_factory_openapi_cases(base: str, artifact_dir: Path, case) -> None:
    """工厂模式多领域（如电商沙箱）通用 HTTP 契约验收：健康检查 + 鉴权守卫 + OpenAPI 端点可达性 + 沙箱支付路由。"""
    health_status, _ = _http("GET", f"{base}/api/health")
    case("factory_health", health_status == 200, f"status={health_status}")

    oa_doc = _read_factory_openapi(artifact_dir)
    paths = oa_doc.get("paths") if isinstance(oa_doc, dict) else None
    has_paths = isinstance(paths, dict) and len(paths) > 0
    case("factory_openapi_valid", has_paths, f"paths={len(paths) if isinstance(paths, dict) else 0}")
    if not has_paths or not isinstance(paths, dict):
        return

    admin_token = _login(base, *_ADMIN)
    member_token = _login(base, *_MEMBER)
    case("login_admin", admin_token is not None)
    case("login_member", member_token is not None)
    probe_token = member_token or admin_token

    probed_get = 0
    probed_sandbox_pay = False
    for raw_path in sorted(paths):
        path_item = paths.get(raw_path)
        if not isinstance(path_item, dict):
            continue
        if not isinstance(raw_path, str) or not raw_path.startswith("/") or "{" in raw_path:
            continue

        get_op = path_item.get("get")
        if isinstance(get_op, dict) and raw_path != "/api/health" and probed_get < 16:
            probed_get += 1
            slug = _slug_path(raw_path)
            requires_auth = _op_requires_auth(get_op)
            if requires_auth:
                unauth_status, _ = _http("GET", f"{base}{raw_path}")
                case(
                    f"factory_auth_guard_{slug}",
                    unauth_status in (401, 403),
                    f"unauth_status={unauth_status}",
                )
            status, _ = _http("GET", f"{base}{raw_path}", probe_token if requires_auth else None)
            case(
                f"factory_get_{slug}",
                status not in (0, 404) and status < 500,
                f"status={status}",
            )

        post_op = path_item.get("post")
        if (
            not probed_sandbox_pay
            and isinstance(post_op, dict)
            and ("sandbox-pay" in raw_path or "payment" in raw_path)
        ):
            probed_sandbox_pay = True
            requires_auth = _op_requires_auth(post_op)
            status, _ = _http(
                "POST",
                f"{base}{raw_path}",
                probe_token if requires_auth else None,
                body={},
            )
            case(
                "factory_sandbox_payment_routed",
                status not in (0, 404) and status < 500,
                f"path={raw_path} status={status}",
            )


def _result(artifact_dir, reference, digest, reconciled, cases) -> AcceptanceResult:
    if any(c.status == "FAIL" for c in cases):
        overall = "FAIL"
    elif any(c.status == "NOT_RUN" for c in cases):
        overall = "NOT_RUN"
    else:
        overall = "PASS"
    return AcceptanceResult(
        artifact_dir=str(artifact_dir),
        mysql_image=reference,
        mysql_digest=digest,
        digest_reconciled=reconciled,
        overall=overall,
        cases=cases,
    )
