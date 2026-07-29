#!/usr/bin/env bash
# AgentOS 每日备份脚本（对应设计文档 19.4 节 / 任务 T6.5）
#
# 功能：
#   1. PostgreSQL 逻辑备份（pg_dump 自定义格式），输出到 data/backups/postgres/<时间戳>.dump
#   2. 上传目录增量备份（tar --listed-incremental），输出到 data/backups/uploads/<时间戳>-uploads.tar.gz
#   3. 保留 14 天，超期备份自动清理
#   4. 执行结果追加写入 data/logs/backup.log，成功退出码 0，失败非 0
#
# 配置：全部从环境变量读取；若仓库根目录存在 .env 则自动加载（内容不会被打印）。
#   默认值与 docker-compose.yml 保持一致，无需任何配置即可直接运行。
#
# 用法：deploy/scripts/backup.sh

set -euo pipefail

# ---------- 路径与配置 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

# 加载 .env（若存在）。注意：绝不打印 .env 内容，避免泄露密码。
if [[ -f "${REPO_ROOT}/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${REPO_ROOT}/.env"
  set +a
fi

# 数据库连接参数（默认值与 docker-compose.yml 一致）
POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"          # Compose 服务名（不发布端口，只能网络内访问）
POSTGRES_DB="${POSTGRES_DB:-agentos}"
POSTGRES_USER="${POSTGRES_USER:-agentos}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-agentos-dev-password}"

# 目录配置（均相对仓库根目录，可用环境变量覆盖）
UPLOADS_DIR="${UPLOADS_DIR:-${REPO_ROOT}/data/uploads}"
BACKUP_DIR="${BACKUP_DIR:-${REPO_ROOT}/data/backups}"
LOG_DIR="${LOG_DIR:-${REPO_ROOT}/data/logs}"
RETENTION_DAYS="${RETENTION_DAYS:-14}"

PG_BACKUP_DIR="${BACKUP_DIR}/postgres"
UP_BACKUP_DIR="${BACKUP_DIR}/uploads"
SNAPSHOT_FILE="${UP_BACKUP_DIR}/.uploads.snar"            # tar 增量备份的快照文件
LOG_FILE="${LOG_DIR}/backup.log"

TS="$(date +%Y%m%d-%H%M%S)"
PG_BACKUP_FILE="${PG_BACKUP_DIR}/${TS}.dump"
UP_BACKUP_FILE="${UP_BACKUP_DIR}/${TS}-uploads.tar.gz"

# ---------- 日志 ----------
log() {
  # 同时输出到终端和日志文件
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "${msg}"
  mkdir -p "${LOG_DIR}"
  echo "${msg}" >> "${LOG_FILE}"
}

fail() {
  log "ERROR: $*"
  exit 1
}

# ---------- 前置检查 ----------
command -v docker >/dev/null 2>&1 || fail "未找到 docker 命令"
cd "${REPO_ROOT}"
docker compose ps --status running --services 2>/dev/null | grep -qx "${POSTGRES_SERVICE}" \
  || fail "postgres 容器未运行，请先 docker compose up -d"
[[ -d "${UPLOADS_DIR}" ]] || fail "上传目录不存在：${UPLOADS_DIR}"

mkdir -p "${PG_BACKUP_DIR}" "${UP_BACKUP_DIR}" "${LOG_DIR}"

log "===== 备份开始（批次 ${TS}） ====="

# ---------- 1. PostgreSQL 逻辑备份（自定义格式，支持 pg_restore 选择性恢复） ----------
log "开始 PostgreSQL 逻辑备份 -> ${PG_BACKUP_FILE}"
if docker compose exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" "${POSTGRES_SERVICE}" \
    pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -Fc > "${PG_BACKUP_FILE}.tmp"; then
  # 基本完整性校验：文件非空且能被 pg_restore 列出目录
  if [[ -s "${PG_BACKUP_FILE}.tmp" ]] \
    && docker compose exec -T "${POSTGRES_SERVICE}" pg_restore -l >/dev/null 2>&1 < "${PG_BACKUP_FILE}.tmp"; then
    mv "${PG_BACKUP_FILE}.tmp" "${PG_BACKUP_FILE}"
    log "PostgreSQL 备份完成，大小 $(du -h "${PG_BACKUP_FILE}" | cut -f1)"
  else
    rm -f "${PG_BACKUP_FILE}.tmp"
    fail "PostgreSQL 备份产物校验失败（文件为空或格式损坏）"
  fi
else
  rm -f "${PG_BACKUP_FILE}.tmp"
  fail "pg_dump 执行失败"
fi

# ---------- 2. 上传目录增量备份 ----------
# 使用 tar --listed-incremental：同一快照文件下，每天只打包新增/变更的文件。
# 恢复演练或全新恢复时，将快照文件删除后的首个备份即为全量基线。
log "开始上传目录增量备份 -> ${UP_BACKUP_FILE}"
if tar --listed-incremental="${SNAPSHOT_FILE}" \
    -czf "${UP_BACKUP_FILE}.tmp" -C "${UPLOADS_DIR}" .; then
  if [[ -s "${UP_BACKUP_FILE}.tmp" ]]; then
    mv "${UP_BACKUP_FILE}.tmp" "${UP_BACKUP_FILE}"
    log "上传目录备份完成，大小 $(du -h "${UP_BACKUP_FILE}" | cut -f1)"
  else
    rm -f "${UP_BACKUP_FILE}.tmp"
    fail "上传目录备份产物为空"
  fi
else
  rm -f "${UP_BACKUP_FILE}.tmp"
  fail "上传目录 tar 备份失败"
fi

# ---------- 3. 保留策略：清理超过 14 天的备份 ----------
log "执行保留策略：清理 ${RETENTION_DAYS} 天前的备份"
deleted=0
while IFS= read -r old; do
  rm -f "${old}"
  log "已清理超期备份：${old}"
  deleted=$((deleted + 1))
done < <(find "${PG_BACKUP_DIR}" "${UP_BACKUP_DIR}" -type f \
  \( -name '*.dump' -o -name '*-uploads.tar.gz' \) -mtime "+${RETENTION_DAYS}")
log "保留策略执行完毕，共清理 ${deleted} 个超期文件"

log "===== 备份完成（批次 ${TS}） ====="
exit 0
