# 平台 UI 镜像：构建静态资源后用 nginx 提供，并把 /api 反代到平台 API。
# 构建上下文为仓库根：docker build -f deploy/ui.Dockerfile .
FROM node:22-slim AS build
WORKDIR /ui
COPY platform-ui/package.json platform-ui/package-lock.json ./
RUN npm ci
COPY platform-ui/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /ui/dist /usr/share/nginx/html
EXPOSE 80
