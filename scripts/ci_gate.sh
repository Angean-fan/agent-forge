#!/usr/bin/env bash
# AgentForge 平台基础回归门禁（M08A）。
#
# 单一口径：CI（Gitee Go 等）应调用本脚本，避免命令散落在流水线里。
# 只覆盖平台自身；不冒充真实 Provider 或 Runner 验收。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> platform-api: uv run --locked pytest"
(cd "${ROOT}/platform-api" && uv run --locked pytest)

echo "==> platform-api: uv run --locked ruff check ."
(cd "${ROOT}/platform-api" && uv run --locked ruff check .)

echo "==> platform-ui: npm test"
(cd "${ROOT}/platform-ui" && npm test)

echo "==> runner: uv run --locked pytest / ruff"
(cd "${ROOT}/runner" && uv run --locked pytest && uv run --locked ruff check .)

echo "==> gate passed"
