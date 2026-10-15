"""C06：Method Unit **编译级认证**（补齐 E10 "只做文本契约校验、未编译" 的缺口）。

在 E10 的人工审核发布流程之外，增加一层**真实构建证据**：把候选 Method Unit 的 ``src/`` 与
``tests/`` 放进一个临时 Maven 工程（受支持 Profile 的 Java 主版本），执行 ``mvn test``，记录
工具链版本、命令、退出码与 JUnit 结果。

约束（对齐《AgentForge_03》C06）：

- **不改变 E10 的人审发布机制**：本模块只产出**证据**，不自动写公共库；发布仍需人工批准；
- **完整构建不可用时明确阻塞**：缺工具链/依赖无法解析 → ``NOT_RUN``（**不**标 PASS）；
- 独立最小调用者 + 真实断言：直接编译并运行单元被认证的测试类，不以 grep/文本相似度替代；
- 不原地修改已发布资产；构建在临时目录进行，不污染仓库。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

STATUS_CERTIFIED = "CERTIFIED"
STATUS_FAILED = "FAILED"
STATUS_NOT_RUN = "NOT_RUN"

DEFAULT_JAVA_RELEASE = "17"
DEFAULT_JUNIT_VERSION = "5.10.1"
DEFAULT_SURFIRE_VERSION = "3.2.5"
DEFAULT_COMPILER_VERSION = "3.11.0"
_BUILD_TIMEOUT_SECONDS = 600

_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)\s*;", re.MULTILINE)
_TEST_SUMMARY = re.compile(r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+)")
_ENV_BLOCKED = (
    "could not resolve",
    "cannot access",
    "pluginresolutionexception",
    "dependencyresolutionexception",
    "non-resolvable",
    "was cached in the local repository",
)


@dataclass(frozen=True)
class Toolchain:
    java: str
    mvn: str

    @property
    def available(self) -> bool:
        return bool(self.java) and bool(self.mvn)

    def to_dict(self) -> dict:
        return {"java": self.java, "maven": self.mvn, "available": self.available}


@dataclass(frozen=True)
class CertificationResult:
    unit_id: str
    version: str
    profile: str
    status: str
    java_release: str
    command: tuple[str, ...]
    exit_code: int | None
    tests_run: int
    failures: int
    errors: int
    toolchain: Toolchain
    notes: tuple[str, ...] = ()

    @property
    def certified(self) -> bool:
        return self.status == STATUS_CERTIFIED

    def evidence(self) -> dict:
        return {
            "unit": f"{self.unit_id}@{self.version}",
            "profile": self.profile,
            "status": self.status,
            "java_release": self.java_release,
            "command": list(self.command),
            "exit_code": self.exit_code,
            "tests_run": self.tests_run,
            "failures": self.failures,
            "errors": self.errors,
            "toolchain": self.toolchain.to_dict(),
            "notes": list(self.notes),
        }


def _cmd_version(executable: str, *args: str) -> str:
    path = shutil.which(executable)
    if path is None:
        return ""
    try:
        output = subprocess.run(
            [executable, *args], capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (output.stdout or output.stderr or "").strip().splitlines()
    return text[0].strip() if text else ""


def detect_toolchain() -> Toolchain:
    """探测本机 Java/Maven（只读；缺任一项 → 认证应标 ``NOT_RUN``）。"""
    return Toolchain(java=_cmd_version("java", "-version"), mvn=_cmd_version("mvn", "-version"))


def _package_of(text: str, *, fallback: str) -> str:
    match = _PACKAGE.search(text)
    return match.group(1) if match else fallback


def _place(source_root: Path, target_root: Path, *, kind: str, notes: list[str]) -> None:
    for path in sorted(source_root.rglob("*.java")):
        text = path.read_text(encoding="utf-8")
        package = _package_of(text, fallback="")
        if not package:
            notes.append(f"缺少 package 声明：{path.name}")
        target = target_root.joinpath(*package.split(".")) if package else target_root
        target.mkdir(parents=True, exist_ok=True)
        (target / path.name).write_text(text, encoding="utf-8")


def _pom_xml(java_release: str) -> str:
    return f"""<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>agentforge.certification</groupId>
  <artifactId>method-unit-certification</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.release>{java_release}</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.junit.jupiter</groupId>
      <artifactId>junit-jupiter</artifactId>
      <version>{DEFAULT_JUNIT_VERSION}</version>
      <scope>test</scope>
    </dependency>
  </dependencies>
  <build>
    <plugins>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-compiler-plugin</artifactId>
        <version>{DEFAULT_COMPILER_VERSION}</version>
      </plugin>
      <plugin>
        <groupId>org.apache.maven.plugins</groupId>
        <artifactId>maven-surefire-plugin</artifactId>
        <version>{DEFAULT_SURFIRE_VERSION}</version>
      </plugin>
    </plugins>
  </build>
</project>
"""


def _parse_report(build_root: Path) -> tuple[int, int, int]:
    reports = build_root / "target" / "surefire-reports"
    tests = failures = errors = 0
    if reports.is_dir():
        for txt in sorted(reports.glob("*.txt")):
            for match in _TEST_SUMMARY.finditer(txt.read_text(encoding="utf-8")):
                tests += int(match.group(1))
                failures += int(match.group(2))
                errors += int(match.group(3))
    return tests, failures, errors


def certify_unit(
    unit_dir: str | Path,
    *,
    profile: str,
    java_release: str = DEFAULT_JAVA_RELEASE,
    workdir: str | Path | None = None,
) -> CertificationResult:
    """对单个 Method Unit 做真实构建认证；返回结构化证据。

    目录约定 ``<unit_dir>/src/*.java`` 与 ``<unit_dir>/tests/*.java``（包名决定落位）。
    """
    unit_dir = Path(unit_dir)
    unit_id = unit_dir.parent.name
    version = unit_dir.name
    toolchain = detect_toolchain()
    src_dir, tests_dir = unit_dir / "src", unit_dir / "tests"
    notes: list[str] = []

    if not toolchain.available:
        return CertificationResult(
            unit_id, version, profile, STATUS_NOT_RUN, java_release, (), None, 0, 0, 0,
            toolchain, ("缺少 Java/Maven 工具链，无法编译认证（阻塞而非 PASS）",),
        )
    if not src_dir.is_dir() or not tests_dir.is_dir():
        return CertificationResult(
            unit_id, version, profile, STATUS_FAILED, java_release, (), None, 0, 0, 0,
            toolchain, ("缺少 src/ 或 tests/ 目录",),
        )

    root = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="cert-"))
    build_root = root / f"{unit_id}-{version}"
    if build_root.exists():
        shutil.rmtree(build_root)
    (build_root / "src/main/java").mkdir(parents=True)
    (build_root / "src/test/java").mkdir(parents=True)
    _place(src_dir, build_root / "src/main/java", kind="main", notes=notes)
    _place(tests_dir, build_root / "src/test/java", kind="test", notes=notes)
    (build_root / "pom.xml").write_text(_pom_xml(java_release), encoding="utf-8")

    command = ("mvn", "-q", "-B", "test")
    try:
        completed = subprocess.run(
            command, cwd=build_root, capture_output=True, text=True,
            timeout=_BUILD_TIMEOUT_SECONDS, check=False,
        )
        output, exit_code = (completed.stdout or "") + (completed.stderr or ""), completed.returncode
    except subprocess.TimeoutExpired:
        return CertificationResult(
            unit_id, version, profile, STATUS_NOT_RUN, java_release, command, None, 0, 0, 0,
            toolchain, (*notes, f"构建超时（>{_BUILD_TIMEOUT_SECONDS}s）"),
        )
    except OSError as exc:
        return CertificationResult(
            unit_id, version, profile, STATUS_NOT_RUN, java_release, command, None, 0, 0, 0,
            toolchain, (*notes, f"无法启动构建：{type(exc).__name__}"),
        )

    tests_run, failures, errors = _parse_report(build_root)
    lowered = output.lower()
    if exit_code == 0 and tests_run > 0 and failures == 0 and errors == 0:
        status = STATUS_CERTIFIED
    elif exit_code != 0 and any(token in lowered for token in _ENV_BLOCKED):
        status = STATUS_NOT_RUN
        notes.append("依赖/插件无法解析，环境受限 → 阻塞而非 PASS")
    else:
        status = STATUS_FAILED
        if tests_run == 0 and exit_code == 0:
            notes.append("未执行任何测试（无可认证断言）")
    return CertificationResult(
        unit_id, version, profile, status, java_release, command, exit_code,
        tests_run, failures, errors, toolchain, tuple(notes),
    )


def certify_published_units(
    methods_root: str | Path, *, profile: str, java_release: str = DEFAULT_JAVA_RELEASE
) -> list[CertificationResult]:
    """认证 ``methods/<id>/<version>/`` 下的全部已发布单元（只读）。"""
    root = Path(methods_root)
    results: list[CertificationResult] = []
    for manifest in sorted(root.glob("*/*/manifest.json")):
        results.append(certify_unit(manifest.parent, profile=profile, java_release=java_release))
    return results


__all__ = [
    "DEFAULT_JAVA_RELEASE",
    "STATUS_CERTIFIED",
    "STATUS_FAILED",
    "STATUS_NOT_RUN",
    "CertificationResult",
    "Toolchain",
    "certify_published_units",
    "certify_unit",
    "detect_toolchain",
]
