## 快速开始

### 环境要求

- Docker Engine 或 Docker Desktop
- Docker Compose 插件
- Docker Buildx 0.17 或更高版本

### 1. 创建环境配置

Linux / macOS：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

首次启动前必须修改 `.env` 中的 `JWT_SECRET`、`POSTGRES_PASSWORD` 和 `BOOTSTRAP_ADMIN_PASSWORD`。bootstrap 是幂等的，不会用后续环境变量覆盖已存在账号的密码。如果启用 Agent，还需要配置 `LLM_PROVIDER`、`LLM_MODEL` 及对应模型服务地址或密钥。

### 2. 构建并启动

```bash
docker compose up -d --build
docker compose ps
```

如果本机安装的是独立版 Compose，请将上述命令中的 `docker compose` 替换为 `docker-compose`。全部服务进入 healthy 状态后即可访问：

- Web：http://localhost:3000
- API：http://localhost:8000
- OpenAPI 文档：http://localhost:8000/docs
- 健康检查：http://localhost:8000/health

### 3. 首次使用

启动过程会幂等创建 `BOOTSTRAP_ADMIN_*` 指定的全局管理员账号与 `BOOTSTRAP_PROJECT_NAME` 指定的默认项目。首次登录后：

1. 在管理控制台创建业务账号，并安全转交仅展示一次的初始密码；
2. 为默认项目指定负责人，或新建项目并指定负责人；
3. 使用负责人账号登录并添加项目成员。

## 常用命令

```bash
# 查看服务状态与日志
docker compose ps
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f scheduler

# 重建单个服务
docker compose up -d --build backend

# 停止服务（保留数据）
docker compose down

# 手动备份与恢复帮助
deploy/scripts/backup.sh
deploy/scripts/restore.sh --help
```

## 测试与构建

```bash
# 后端测试（测试库会自动执行 Alembic 升级）
docker compose run --rm --no-deps -v "./backend:/app" backend python -m pytest tests/ -q

# 前端测试与生产构建
cd frontend
npm ci
npm test
npm run build
```
