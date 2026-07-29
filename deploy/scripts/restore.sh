#!/usr/bin/env bash
# AgentOS 恢复脚本（对应设计文档 19.4 节 / 任务 T6.5）
#
# 功能：
#   1. 从 pg_dump 自定义格式备份恢复数据库，支持恢复到任意指定库名
#      （默认要求恢复到非主库；恢复到主库必须显式 --confirm，避免误覆盖）
#   2. 从上传目录 tar 备份恢复文件到指定目录
#   3. 恢复后校验：库连通、核心表存在、文件 SHA-256 与 stored_files 记录抽查比对
#
# 用法示例：
#   # 恢复到全新库与全新目录（恢复演练场景）
#   deploy/scripts/restore.sh \
#     --dump data/backups/postgres/20260728-120000.dump \
#     --target-db agentos_restore_drill \
#     --uploads-archive data/backups/uploads/20260728-120000-uploads.tar.gz \
#     --uploads-target data/restore-drill/uploads
#
#   # 恢复覆盖主库（危险操作，必须 --confirm）
#   deploy/scripts/restore.sh --dump <备份文件> --target-db agentos \
#     --uploads-archive <备份文件> --uploads-target data/uploads --confirm
#
# 退出码：0 成功且校验通过；非 0 失败。

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

POSTGRES_SERVICE="${POSTGRES_SERVICE:-postgres}"
POSTGRES_DB="${POSTGRES_DB:-agentos}"
POSTGRES_USER="${POSTGRES_USER:-agentos}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-agentos-dev-password}"
LOG_DIR="${LOG_DIR:-${REPO_ROOT}/data/logs}"
LOG_FILE="${LOG_DIR}/restore.log"

# SHA-256 抽查样本数
VERIFY_SAMPLE_SIZE="${VERIFY_SAMPLE_SIZE:-20}"

# ---------- 参数解析 ----------
DUMP_FILE=""
TARGET_DB=""
UPLOADS_ARCHIVE=""
UPLOADS_TARGET=""
CONFIRM=0

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}"
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump) DUMP_FILE="$2"; shift 2 ;;
    --target-db) TARGET_DB="$2"; shift 2 ;;
    --uploads-archive) UPLOADS_ARCHIVE="$2"; shift 2 ;;
    --uploads-target) UPLOADS_TARGET="$2"; shift 2 ;;
    --confirm) CONFIRM=1; shift ;;
    -h|--help) usage ;;
    *) echo "未知参数：$1" >&2; usage ;;
  esac
done

[[ -n "${DUMP_FILE}" && -n "${TARGET_DB}" ]] || usage
# 库名合法性校验（会拼进 SQL，必须防注入）
[[ "${TARGET_DB}" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || { echo "非法库名：${TARGET_DB}" >&2; exit 2; }
# 文件与目录配套校验
if [[ -n "${UPLOADS_ARCHIVE}" || -n "${UPLOADS_TARGET}" ]]; then
  [[ -n "${UPLOADS_ARCHIVE}" && -n "${UPLOADS_TARGET}" ]] \
    || { echo "--uploads-archive 与 --uploads-target 必须同时提供" >&2; exit 2; }
fi

log() {
  local msg="[$(date '+%Y-%m-%d %H:%M:%S')] $*"
  echo "${msg}"
  mkdir -p "${LOG_DIR}"
  echo "${msg}" >> "${LOG_FILE}"
}

fail() {
  log "ERROR: $*"
  exit 1
}

# 在 postgres 容器内执行 psql/pg_restore（Compose 网络内访问，无需发布端口）
psql_admin() {
  docker compose exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" "${POSTGRES_SERVICE}" \
    psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 "$@"
}
psql_target() {
  docker compose exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" "${POSTGRES_SERVICE}" \
    psql -U "${POSTGRES_USER}" -d "${TARGET_DB}" -v ON_ERROR_STOP=1 "$@"
}

# ---------- 前置检查 ----------
command -v docker >/dev/null 2>&1 || fail "未找到 docker 命令"
cd "${REPO_ROOT}"
docker compose ps --status running --services 2>/dev/null | grep -qx "${POSTGRES_SERVICE}" \
  || fail "postgres 容器未运行"
[[ -f "${DUMP_FILE}" ]] || fail "数据库备份文件不存在：${DUMP_FILE}"
if [[ -n "${UPLOADS_ARCHIVE}" ]]; then
  [[ -f "${UPLOADS_ARCHIVE}" ]] || fail "上传目录备份文件不存在：${UPLOADS_ARCHIVE}"
fi

# ---------- 覆盖保护 ----------
if [[ "${TARGET_DB}" == "${POSTGRES_DB}" && "${CONFIRM}" -ne 1 ]]; then
  fail "目标库是主库 ${POSTGRES_DB}！覆盖主库属于危险操作，如确认无误请追加 --confirm"
fi

log "===== 恢复开始：${DUMP_FILE} -> 库 ${TARGET_DB} ====="

# ---------- 1. 恢复数据库 ----------
# 无论目标库是否存在，都重建为空库，保证恢复结果只来自备份（恢复到全新库时等价于新建）
log "重建目标库 ${TARGET_DB}（DROP IF EXISTS + CREATE）"
psql_admin -c "DROP DATABASE IF EXISTS \"${TARGET_DB}\";" >/dev/null
psql_admin -c "CREATE DATABASE \"${TARGET_DB}\";" >/dev/null

log "执行 pg_restore"
if ! docker compose exec -T -e PGPASSWORD="${POSTGRES_PASSWORD}" "${POSTGRES_SERVICE}" \
    pg_restore -U "${POSTGRES_USER}" -d "${TARGET_DB}" --no-owner --exit-on-error \
    < "${DUMP_FILE}"; then
  fail "pg_restore 执行失败"
fi
log "数据库恢复完成"

# ---------- 2. 恢复上传目录 ----------
if [[ -n "${UPLOADS_ARCHIVE}" ]]; then
  if [[ "${UPLOADS_TARGET}" == "${REPO_ROOT}/data/uploads" || "${UPLOADS_TARGET}" == "data/uploads" ]] \
     && [[ "${CONFIRM}" -ne 1 ]]; then
    fail "目标目录是线上上传目录 data/uploads！覆盖它需要追加 --confirm"
  fi
  log "恢复上传目录 -> ${UPLOADS_TARGET}"
  mkdir -p "${UPLOADS_TARGET}"
  # 备份时以 -C data/uploads . 打包，直接解包到目标目录即可
  tar -xzf "${UPLOADS_ARCHIVE}" -C "${UPLOADS_TARGET}"
  log "上传目录恢复完成，共 $(find "${UPLOADS_TARGET}" -type f | wc -l) 个文件"
fi

# ---------- 3. 恢复后校验 ----------
log "开始恢复后校验"

# 3.1 库连通性
psql_target -c "SELECT 1;" >/dev/null || fail "校验失败：无法连接目标库 ${TARGET_DB}"
log "校验通过：目标库可连接"

# 3.2 核心表存在
for tbl in users projects project_members work_items stored_files; do
  exists="$(psql_target -tAc "SELECT to_regclass('public.${tbl}') IS NOT NULL;")"
  [[ "${exists}" == "t" ]] || fail "校验失败：核心表 ${tbl} 不存在"
done
log "校验通过：核心表 users/projects/project_members/work_items/stored_files 均存在"

# 3.3 文件 SHA-256 与 stored_files 记录抽查比对
if [[ -n "${UPLOADS_TARGET}" ]]; then
  total_files="$(psql_target -tAc "SELECT count(*) FROM stored_files;")"
  log "stored_files 共 ${total_files} 条记录，随机抽查最多 ${VERIFY_SAMPLE_SIZE} 条"
  ok=0; bad=0; missing=0
  while IFS='|' read -r storage_key sha256; do
    [[ -n "${storage_key}" ]] || continue
    f="${UPLOADS_TARGET}/${storage_key}"
    if [[ ! -f "${f}" ]]; then
      log "  缺失文件：${storage_key}"
      missing=$((missing + 1)); continue
    fi
    actual="$(sha256sum "${f}" | cut -d' ' -f1)"
    if [[ "${actual}" == "${sha256}" ]]; then
      ok=$((ok + 1))
    else
      log "  哈希不一致：${storage_key} 期望 ${sha256} 实际 ${actual}"
      bad=$((bad + 1))
    fi
  done < <(psql_target -tAc "SELECT storage_key, sha256 FROM stored_files ORDER BY random() LIMIT ${VERIFY_SAMPLE_SIZE};")
  log "SHA-256 抽查结果：一致 ${ok}，不一致 ${bad}，文件缺失 ${missing}"
  [[ "${bad}" -eq 0 && "${missing}" -eq 0 ]] || fail "校验失败：存在哈希不一致或缺失文件"
  log "校验通过：抽查文件 SHA-256 全部与 stored_files 记录一致"
fi

log "===== 恢复完成，全部校验通过 ====="
exit 0
