#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────────
# entrypoint.sh — Patroni 容器啟動腳本
#
# 以 root 執行（postgres:15 預設），完成以下步驟後以 postgres 身份執行 Patroni：
#   1. 確保 PostgreSQL 資料目錄存在並屬於 postgres 用戶
#   2. 確保必要目錄與腳本可執行
#   3. exec gosu postgres patroni（從 root 降權至 postgres）
# ──────────────────────────────────────────────────────────────────────────────
set -e

DATA_DIR="${PATRONI_POSTGRESQL_DATA_DIR:-/data/patroni}"

echo "[entrypoint] 建立資料目錄: ${DATA_DIR}"
mkdir -p "${DATA_DIR}"
chown -R postgres:postgres "${DATA_DIR}"
chmod 750 "${DATA_DIR}"

echo "[entrypoint] 啟動 Patroni（節點: ${PATRONI_NAME:-unknown}）"
exec gosu postgres patroni /etc/patroni/patroni.yml
