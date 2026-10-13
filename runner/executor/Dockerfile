# Runner 执行镜像：构建时预热固定模板依赖，运行时保持 --network none。
# 从仓库根目录执行：docker build -f runner/executor/Dockerfile -t agentforge/runner-executor:demo-0.2 .
FROM maven:3.9-eclipse-temurin-17

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

COPY runner/executor/scripts/ /opt/agentforge/run/
COPY templates/ /opt/agentforge/prewarm/templates/
RUN chmod +x /opt/agentforge/run/*.sh \
 && mkdir -p /opt/agentforge/maven-cache /opt/agentforge/npm-cache \
 && for project in demo-0.1 demo-0.2 demo-0.2-js demo-0.2-mb demo-0.2-js-mb; do \
      mvn -B -q -f "/opt/agentforge/prewarm/templates/$project/backend/pom.xml" \
        -Dmaven.repo.local=/opt/agentforge/maven-cache dependency:go-offline; \
      mvn -B -q -f "/opt/agentforge/prewarm/templates/$project/backend/pom.xml" \
        -Dmaven.repo.local=/opt/agentforge/maven-cache -DskipTests package; \
      mvn -B -q -f "/opt/agentforge/prewarm/templates/$project/backend/pom.xml" \
        -Dmaven.repo.local=/opt/agentforge/maven-cache test; \
      cd "/opt/agentforge/prewarm/templates/$project/frontend"; \
      npm ci --ignore-scripts --no-audit --no-fund --cache /opt/agentforge/npm-cache; \
    done \
 && rm -rf /opt/agentforge/prewarm \
 && chmod -R a+rX /opt/agentforge/maven-cache /opt/agentforge/npm-cache

# 能力包装配时加入的依赖不在基础模板 pom 中，必须单独预热。
RUN mvn -B -q -Dmaven.repo.local=/opt/agentforge/maven-cache \
      dependency:get -Dartifact=com.baomidou:mybatis-plus-jsqlparser:3.5.17 \
 && chmod -R a+rX /opt/agentforge/maven-cache

# 以非 root 运行（与 Runner 的 --user 一致）
USER 1000:1000
WORKDIR /workspace

# 运行期只读缓存；日志写入容器 tmpfs，不访问公网依赖仓库。
ENV HOME=/tmp
ENV MAVEN_CONFIG=/tmp/.m2
ENV MAVEN_OPTS="-Djansi.mode=off"
ENV npm_config_cache=/opt/agentforge/npm-cache
ENV npm_config_offline=true
ENV npm_config_logs_dir=/tmp/.npm-logs
