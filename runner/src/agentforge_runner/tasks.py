"""固定任务白名单。

Runner **只**执行这里登记的任务与命令，永不接受调用方传入的命令或脚本；
需要多步的构建用镜像内**固定脚本**（`/opt/agentforge/run/*.sh`）承载，而不是任意 shell。

M16 只登记构建类任务；MySQL 初始化与启动/HTTP 验收任务在 M18 引入。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Task:
    name: str
    workdir: str
    command: list[str]
    timeout_seconds: int = 900
    description: str = ""


TASKS: dict[str, Task] = {
    "smoke": Task(
        name="smoke",
        workdir="/workspace",
        command=["java", "-version"],
        timeout_seconds=120,
        description="受控容器启动/清理冒烟检查",
    ),
    "backend_test": Task(
        name="backend_test",
        workdir="/workspace/backend",
        command=["mvn", "-B", "-o", "-Dmaven.repo.local=/opt/agentforge/maven-cache", "test"],
        description="后端单元/集成测试",
    ),
    "backend_build": Task(
        name="backend_build",
        workdir="/workspace/backend",
        command=["mvn", "-B", "-o", "-Dmaven.repo.local=/opt/agentforge/maven-cache",
                 "-DskipTests", "package"],
        description="后端打包",
    ),
    "frontend_build": Task(
        name="frontend_build",
        workdir="/workspace/frontend",
        command=["/opt/agentforge/run/frontend_build.sh"],
        description="前端 npm ci + 类型检查 + 构建（固定脚本）",
    ),
}


def get_task(name: str) -> Task | None:
    return TASKS.get(name)
