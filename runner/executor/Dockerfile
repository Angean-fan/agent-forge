# Runner 执行镜像：在受控容器内构建/验收生成代码（JDK17 + Maven + Node22）。
#
# 本机无 Docker 时无法构建；需在受控 Linux + Docker 环境构建，并把实际镜像 digest
# 回填到新的 Platform Profile（见 docs/M16_运行器记录.md）。
# 基础镜像当前用 tag，构建时须改为不可变 digest。
FROM maven:3.9-eclipse-temurin-17

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

COPY scripts/ /opt/agentforge/run/
RUN chmod +x /opt/agentforge/run/*.sh

# 以非 root 运行（与 Runner 的 --user 一致）
USER 1000:1000
WORKDIR /workspace
