#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# post_bootstrap.sh — 叢集初始化後建立應用程式資料庫
#
# 由 Patroni 在第一次 Bootstrap 成功後（initdb 完成、postgres 已啟動）呼叫。
# 此時 postgres superuser 已可連線，執行以下初始化：
#   1. 建立 appuser（若 bootstrap.users 已建立則忽略錯誤）
#   2. 建立 appdb 資料庫
#   3. 授予 appuser 在 appdb 的完整權限
# ──────────────────────────────────────────────────────────────────────────────
set -e

export PGPASSWORD="${PATRONI_SUPERUSER_PASSWORD:-postgres123}"
PG_HOST="localhost"
PG_PORT="5432"
PG_USER="${PATRONI_SUPERUSER_USERNAME:-postgres}"

echo "[post_bootstrap] 等待 PostgreSQL 準備就緒..."
until pg_isready -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" 2>/dev/null; do
    sleep 1
done
echo "[post_bootstrap] PostgreSQL 已就緒，開始初始化應用程式資料庫..."

# 建立 appuser（若已存在則忽略）
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" \
    -c "CREATE USER appuser WITH PASSWORD 'apppassword' CREATEDB;" \
    2>/dev/null || echo "[post_bootstrap] appuser 已存在，略過建立"

# 建立 appdb（若已存在則忽略）
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" \
    -c "CREATE DATABASE appdb OWNER appuser;" \
    2>/dev/null || echo "[post_bootstrap] appdb 已存在，略過建立"

# 授予 schema 權限
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" -d appdb \
    -c "GRANT ALL ON SCHEMA public TO appuser;" \
    2>/dev/null || true

# 讓 postgres 超級用戶可存取 appdb（供 postgres_exporter 使用）
psql -h "${PG_HOST}" -p "${PG_PORT}" -U "${PG_USER}" \
    -c "GRANT ALL PRIVILEGES ON DATABASE appdb TO ${PG_USER};" \
    2>/dev/null || true

echo "[post_bootstrap] 應用程式資料庫初始化完成。"
echo "[post_bootstrap]   appdb    → 已建立，owner = appuser"
echo "[post_bootstrap]   appuser  → password = apppassword"
