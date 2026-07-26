# 构建阶段：Node 仅存在于镜像内，不依赖宿主机工具链
FROM node:20-alpine AS build
# 默认官方源；国内网络可在 compose build args 中覆盖为镜像站
ARG NPM_REGISTRY=https://registry.npmjs.org
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm config set registry "${NPM_REGISTRY}" \
    && npm config set fetch-retries 5 \
    && npm config set fetch-retry-factor 2 \
    && npm config set fetch-retry-maxtimeout 120000 \
    && npm config set fetch-timeout 600000 \
    && npm ci --no-audit --no-fund
COPY . .
RUN npm run build

# 运行阶段：nginx 提供 Web 入口并反向代理 /api
FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 3000
CMD ["nginx", "-g", "daemon off;"]
