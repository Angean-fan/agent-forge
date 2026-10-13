"""R27 图书借阅固定业务主线与资产版本回归。"""

from __future__ import annotations

from pathlib import Path

from agentforge_platform.assembler import assemble_from_spec
from agentforge_platform.coverage import platform_mainline
from agentforge_platform.spec import SoftwareSpecification

REPO = Path(__file__).resolve().parents[2]


def test_lending_is_fixed_mainline_with_atomic_borrow_asset(tmp_path: Path) -> None:
    spec = SoftwareSpecification.model_validate_json(
        (REPO / "schemas" / "book-lending.spec.json").read_text(encoding="utf-8")
    )
    assert platform_mainline(spec) == "book_lending"

    target = tmp_path / "lending"
    assemble_from_spec(spec, target)
    loan_service = (target / "backend/src/main/java/com/agentforge/template/service/LoanService.java")
    text = loan_service.read_text(encoding="utf-8")
    assert "FOR UPDATE" in text
    assert '"BORROWED"' in text
    assert '"RETURNED"' in text
    assert "@Transactional" in text
    assert (target / "backend/src/test/java/com/agentforge/template/LoanApiTest.java").is_file()
