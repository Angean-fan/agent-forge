#!/usr/bin/env bash
# 固定脚本：前端依赖安装 + 类型检查 + 构建。仅在受限容器内、/workspace 下执行。
set -euo pipefail
cd /workspace/frontend
npm ci --offline --no-audit --no-fund
npm run type-check
npm run build
