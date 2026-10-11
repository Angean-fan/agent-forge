# runner

AgentForge 的**独立受控 Runner**：在隔离容器内执行生成代码的构建/启动验收。
平台 API 不持有 Docker 权限，只通过内部认证的 Runner API 提交运行。

- 引入步骤：M16
- 认证：除 `/health` 外，所有请求须携带 `X-Runner-Token`（来自环境变量 `RUNNER_TOKEN`）
- 执行：只经**固定任务白名单**（`tasks.py`），把任务翻译成受限 `docker run`；**不暴露任意 shell**
- 工件：`artifact_dir` 必须是 `RUNNER_WORK_ROOT` 之内的相对路径（拒绝越界与符号链接逃逸）

## 隔离契约（`container.build_run_argv`）

`--network none`、`--read-only` + `--tmpfs /tmp`、`--user 1000:1000`、`--cap-drop ALL`、
`--security-opt no-new-privileges`、`--pids-limit`、`--memory`、`--cpus`；
**只挂载本次 run 的工件目录**，不挂载宿主 docker socket / 平台数据库 / 模型密钥 / 宿主敏感目录。

## 运行

```bash
cd runner
uv sync --dev
uv run uvicorn agentforge_runner.main:app --port 9000
```

环境变量：`RUNNER_TOKEN`（必填）、`RUNNER_WORK_ROOT`、`RUNNER_EXECUTOR_IMAGE`。

## 执行镜像

`executor/Dockerfile` + `executor/scripts/` 定义执行镜像（JDK17 + Maven + Node22）。
**本机无 Docker，镜像未构建、digest 未回填**；须在受控 Linux 环境构建后回填到 Profile。

## 测试

```bash
uv run pytest        # 认证/白名单/路径/降级/隔离参数契约（不依赖 Docker）
```
