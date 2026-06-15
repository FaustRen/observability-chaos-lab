#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
user_api.py — 模擬真實使用者行為的 FastAPI 獨立服務

架構定位：
  - 運行於 Host 端 localhost:8001（非 Docker 容器）
  - 連接 Docker 服務：PostgreSQL localhost:5432、Redis localhost:6379
  - 對外暴露 /metrics，供 Prometheus 監控（需更新 prometheus.yml）
  - 使用 logger_manager.log_decorator 對每個 service function 記錄 file log
  - 每筆 API 請求結束後同步寫一筆 request log 至 PostgreSQL user_request_logs 表

資料庫設計（新增 2 張表）：
  customers          → 客戶主資料（name / email / phone）
  user_request_logs  → 每筆 API 呼叫的執行紀錄

API 端點設計：
  GET  /customer/search?name=Alice      → 查詢客戶（Cache-aside: Redis → PostgreSQL）
  POST /customer/query                  → 複合條件查詢（非 insert，body 帶查詢條件）
  POST /customer/register               → 新增客戶（insert into customers）

啟動方式：
  python user_api.py
"""

import random
import threading
import time
import logging
import os
from contextlib import asynccontextmanager

import redis as redis_lib
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse
from prometheus_client import Counter, Gauge, Histogram, make_asgi_app
from pydantic import BaseModel
from sqlalchemy import create_engine, text

from logger_manager import setup_logging, log_decorator

# ──────────────────────────────────────────────────────────────────────────────
# 設定
# ──────────────────────────────────────────────────────────────────────────────
DB_URL           = "postgresql://appuser:apppassword@localhost:5432/appdb"
# Redis：各節點對應的 host port（redis-master:6379, replica1:6380, replica2:6381）
# Sentinel failover 後 master 可能換到 6380/6381，啟動時自動掃描
_REDIS_PORTS     = [6379, 6380, 6381]
CACHE_TTL        = 30          # Redis cache TTL（秒）
CACHE_PREFIX = "customer:" # Redis key 前綴

logger = setup_logging("user_api")

# 為每個被 @log_decorator 裝飾的 service function 個別初始化 logger
# （decorator 用 func.__name__ 作 logger name，需預先 setup 才會寫到 .log 檔）
setup_logging("_search_customers_by_name")
setup_logging("_query_customers_compound")
setup_logging("_register_new_customer")

# ──────────────────────────────────────────────────────────────────────────────
# Prometheus Metrics（獨立於主 app，避免指標名稱衝突）
# ──────────────────────────────────────────────────────────────────────────────
_req_count = Counter(
    "user_api_requests_total",
    "Total HTTP requests received by user_api",
    ["method", "endpoint", "status_code"],
)
_req_latency = Histogram(
    "user_api_request_duration_seconds",
    "Response time of user_api endpoints",
    ["method", "endpoint"],
    # 更細的 bucket 分佈，讓 P50/P90/P99 差距在 chaos 時清晰可見
    buckets=[0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.5, 0.75, 1.0, 2.0, 3.0],
)

# ── Redis Cache 指標 ─────────────────────────────────────────────────────────
# 對應主 app 的 redis_cache_hits_total / redis_cache_misses_total，
# 但用不同的名稱以避免 Prometheus 命名衝突
_cache_hits = Counter(
    "user_api_cache_hits_total",
    "Redis cache hits in user_api service",
    ["endpoint"],
)
_cache_misses = Counter(
    "user_api_cache_misses_total",
    "Redis cache misses in user_api service",
    ["endpoint"],
)

# ── DB 操作指標 ───────────────────────────────────────────────────────────────
_db_query_duration = Histogram(
    "user_api_db_query_duration_seconds",
    "PostgreSQL query duration in user_api service",
    ["query_type"],
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0],
)

# ── Chaos / Log Storm 狀態指標 ────────────────────────────────────────────────
# 對應主 app 的 log_storm_active、log_writes_total、log_write_duration_seconds
_log_storm_active_gauge = Gauge(
    "user_api_log_storm_active",
    "1 if user_api log storm simulation is active, 0 otherwise",
)
_log_writes = Counter(
    "user_api_log_writes_total",
    "Number of synchronous DB log writes performed by user_api (including chaos amplification)",
)
_log_write_duration = Histogram(
    "user_api_log_write_duration_seconds",
    "Duration of each log write batch (including chaos-induced delay)",
    buckets=[0.001, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
)

# ── Chaos 全域狀態（執行緒安全）──────────────────────────────────────────────
_log_storm_event = threading.Event()    # set() → 啟動; clear() → 停止

# ──────────────────────────────────────────────────────────────────────────────
# DB & Cache 連線
# ──────────────────────────────────────────────────────────────────────────────
_engine     = create_engine(DB_URL, pool_pre_ping=True, pool_size=20)
_redis_lock = threading.Lock()


def _connect_redis_master() -> redis_lib.Redis:
    """
    掃描已知 host port（6379/6380/6381）找出目前 Redis master 並建立連線。
    Sentinel failover 後 master 可能換到 replica 節點（port 6380 或 6381）。
    """
    for port in _REDIS_PORTS:
        try:
            r = redis_lib.Redis(
                host="localhost", port=port,
                decode_responses=True, socket_connect_timeout=2,
            )
            if r.info("replication").get("role") == "master":
                logger.info("Redis master found at localhost:%d", port)
                return r
        except Exception:
            pass
    logger.warning("Redis master not found on any known port; falling back to localhost:6379")
    return redis_lib.Redis(host="localhost", port=6379, decode_responses=True, socket_connect_timeout=2)


_redis: redis_lib.Redis = _connect_redis_master()


def _redis_set(key: str, value: str, **kwargs) -> None:
    """
    寫入 Redis cache；若遇到 ReadOnlyError（Sentinel failover 後連到 replica）
    則自動重新找 master 並重試一次。失敗為非致命錯誤（下次請求會 cache miss）。
    """
    global _redis
    try:
        _redis.set(key, value, **kwargs)
    except redis_lib.exceptions.ReadOnlyError:
        logger.warning("Redis write rejected (read-only replica); reconnecting to master")
        with _redis_lock:
            _redis = _connect_redis_master()
        _redis.set(key, value, **kwargs)
    except Exception as exc:
        logger.warning("Redis SET failed (non-fatal, will cache-miss next request): %s", exc)


def _wait_for_db_ready() -> None:
    """
    啟動時等待 DB 可連線，避免 Patroni/HAProxy 切換期間造成 app 直接啟動失敗。
    """
    retries = int(os.getenv("DB_STARTUP_RETRIES", "20"))
    interval = float(os.getenv("DB_STARTUP_RETRY_INTERVAL", "2"))

    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            with _engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.commit()
            if attempt > 1:
                logger.info("DB became ready after %s attempts", attempt)
            return
        except Exception as exc:
            last_exc = exc
            logger.warning(
                "DB not ready (%s/%s): %s",
                attempt,
                retries,
                str(exc),
            )
            if attempt < retries:
                time.sleep(interval)

    raise RuntimeError(f"DB not ready after {retries} attempts") from last_exc


def _init_tables() -> None:
    """建立 customers、user_request_logs 兩張表並填入種子資料。"""
    with _engine.connect() as conn:
        # ── customers 表（客戶主資料）────────────────────────────────────────
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS customers (
                id         SERIAL PRIMARY KEY,
                name       VARCHAR(100) NOT NULL,
                email      VARCHAR(200) NOT NULL UNIQUE,
                phone      VARCHAR(20),
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))

        # ── user_request_logs 表（API 呼叫 log）──────────────────────────────
        # 記錄來自 user_api 每筆請求的完整執行狀況，
        # 與 app/ 內的 api_logs 分開，便於區分「內部系統 log」vs「使用者端 log」
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS user_request_logs (
                id               SERIAL PRIMARY KEY,
                endpoint         VARCHAR(200) NOT NULL,
                method           VARCHAR(10)  NOT NULL,
                func_name        VARCHAR(100),
                args_summary     TEXT,
                result_summary   TEXT,
                error_message    TEXT,
                latency_ms       FLOAT        NOT NULL,
                status_code      INTEGER      NOT NULL,
                created_at       TIMESTAMP DEFAULT NOW()
            )
        """))

        # 填入 5 筆種子客戶（僅第一次啟動時）
        count = conn.execute(text("SELECT COUNT(*) FROM customers")).scalar()
        if count == 0:
            seed_data = [
                ("Alice Wang",  "alice@example.com",  "0912-001-001"),
                ("Bob Chen",    "bob@example.com",    "0912-002-002"),
                ("Carol Lin",   "carol@example.com",  "0912-003-003"),
                ("David Huang", "david@example.com",  "0912-004-004"),
                ("Eva Chang",   "eva@example.com",    "0912-005-005"),
            ]
            for name, email, phone in seed_data:
                conn.execute(
                    text("INSERT INTO customers (name, email, phone) VALUES (:n, :e, :p)"),
                    {"n": name, "e": email, "p": phone},
                )
            logger.info("Seeded 5 customers into 'customers' table")

        conn.commit()
    logger.info("DB tables initialized: customers, user_request_logs")


def _write_request_log(
    endpoint: str,
    method: str,
    func_name: str,
    args_summary: str,
    result_summary: str,
    error_message: str | None,
    latency_ms: float,
    status_code: int,
) -> None:
    """
    將一筆 API 呼叫的執行結果同步寫入 user_request_logs。

    Chaos Log Storm 模式啟動時（_log_storm_event.is_set()）：
      - 加入 50~200ms 的隨機延遲，模擬 DB contention under high load
      - 每筆請求額外寫入 4 筆重複 log（共 5 筆），放大 DB 寫入壓力
      - 這些操作都在 HTTP 請求的回應路徑上，因此 middleware 計時會包含這段延遲，
        scatter plot 的 Y 軸值（latency）會顯著上升
    """
    # 非 Log Storm 模式下直接返回，保持 Write Rate 基線為 0
    # Log Storm 啟動後才開始寫入並計數，讓面板有明確的 before/after 對比
    if not _log_storm_event.is_set():
        return

    t_write_start = time.perf_counter()

    # 模擬 DB 同步寫 log 在高並發下產生的競爭延遲（50~200ms）
    time.sleep(random.uniform(0.05, 0.20))

    log_params = {
        "endpoint": endpoint,
        "method":   method,
        "func_name": func_name,
        "args":     args_summary[:500] if args_summary else "",
        "result":   result_summary[:500] if result_summary else "",
        "error":    error_message,
        "latency":  latency_ms,
        "status":   status_code,
    }
    insert_sql = text("""
        INSERT INTO user_request_logs
          (endpoint, method, func_name, args_summary,
           result_summary, error_message, latency_ms, status_code)
        VALUES
          (:endpoint, :method, :func_name, :args,
           :result, :error, :latency, :status)
    """)

    # Log Storm 模式：寫 5 筆（1 正常 + 4 amplification），放大 DB 寫入壓力
    try:
        with _engine.connect() as conn:
            for _ in range(5):
                conn.execute(insert_sql, log_params)
            conn.commit()
        _log_writes.inc(5)
    except Exception as exc:
        logger.warning("Failed to write request log to DB: %s", exc)
    finally:
        _log_write_duration.observe(time.perf_counter() - t_write_start)


# ──────────────────────────────────────────────────────────────────────────────
# Pydantic Request Bodies
# ──────────────────────────────────────────────────────────────────────────────

class CustomerQueryBody(BaseModel):
    """POST /customer/query 的請求 body（只查詢，不 insert）"""
    name:  str = ""
    email: str = ""


class CustomerRegisterBody(BaseModel):
    """POST /customer/register 的請求 body（insert 新客戶）"""
    name:  str
    email: str
    phone: str = ""


# ──────────────────────────────────────────────────────────────────────────────
# Service Functions（套 @log_decorator 記錄 file log）
# ──────────────────────────────────────────────────────────────────────────────

@log_decorator
def _search_customers_by_name(name: str) -> dict:
    """
    GET 查詢的 service 邏輯：Cache-aside 模式。
    先查 Redis；miss 才走 PostgreSQL，結果回寫 Redis（TTL=30s）。
    同時更新 user_api_cache_hits_total / user_api_cache_misses_total，
    讓 Redis 層指標在 Grafana 有對應資料（可加入面板 PromQL）。
    """
    cache_key = f"{CACHE_PREFIX}search:{name.lower()}"

    cached = _redis.get(cache_key)
    if cached:
        _cache_hits.labels(endpoint="/customer/search").inc()
        return {"source": "cache (Redis)", "data": cached, "count": "cached"}

    _cache_misses.labels(endpoint="/customer/search").inc()
    t0 = time.perf_counter()
    with _engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, email, phone FROM customers WHERE name ILIKE :p LIMIT 20"),
            {"p": f"%{name}%"},
        ).fetchall()
    _db_query_duration.labels(query_type="search").observe(time.perf_counter() - t0)

    data = [{"id": r[0], "name": r[1], "email": r[2], "phone": r[3]} for r in rows]
    _redis_set(cache_key, str(data), ex=CACHE_TTL)
    return {"source": "database (PostgreSQL)", "data": data, "count": len(data)}


@log_decorator
def _query_customers_compound(name: str, email: str) -> dict:
    """
    POST 查詢的 service 邏輯：複合條件查詢（不 insert 任何資料）。
    模擬前端送出複合篩選條件（如搜尋頁面送出 POST）。
    """
    conditions, params = [], {}
    if name:
        conditions.append("name ILIKE :name")
        params["name"] = f"%{name}%"
    if email:
        conditions.append("email ILIKE :email")
        params["email"] = f"%{email}%"

    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    t0 = time.perf_counter()
    with _engine.connect() as conn:
        rows = conn.execute(
            text(f"SELECT id, name, email, phone FROM customers {where} LIMIT 20"),
            params,
        ).fetchall()
    _db_query_duration.labels(query_type="query").observe(time.perf_counter() - t0)
    data = [{"id": r[0], "name": r[1], "email": r[2], "phone": r[3]} for r in rows]
    return {"source": "database (PostgreSQL)", "data": data, "count": len(data)}


@log_decorator
def _register_new_customer(name: str, email: str, phone: str) -> dict:
    """
    POST register 的 service 邏輯：將新客戶 INSERT 進 customers 表。
    成功回傳新建立的 id；email 重複時回傳 conflict 訊息。
    """
    t0 = time.perf_counter()
    with _engine.connect() as conn:
        row = conn.execute(
            text("""
                INSERT INTO customers (name, email, phone)
                VALUES (:name, :email, :phone)
                RETURNING id, name, email, created_at
            """),
            {"name": name, "email": email, "phone": phone},
        ).fetchone()
        conn.commit()
    _db_query_duration.labels(query_type="register").observe(time.perf_counter() - t0)
    return {
        "status": "created",
        "id":    row[0],
        "name":  row[1],
        "email": row[2],
        "created_at": str(row[3]),
    }


# ──────────────────────────────────────────────────────────────────────────────
# FastAPI App
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _wait_for_db_ready()
    _init_tables()
    logger.info("user_api is ready at http://localhost:8001")
    yield
    logger.info("user_api shutting down")


app = FastAPI(
    lifespan=lifespan,
    title="User API Simulator",
    description="模擬真實使用者 GET / POST 呼叫的 FastAPI 服務",
    version="1.0.0",
)

# Prometheus /metrics 端點
app.mount("/metrics", make_asgi_app())


# ── HTTP 中介層：計時 + Prometheus 指標 ────────────────────────────────────────
# 注意：只記錄業務端點，排除 /metrics（Prometheus scrape）與 /health（心跳）
# 避免 scrape 自身的噪音指標污染散點圖視覺化
@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    path = request.url.path
    # 略過 Prometheus scrape 路徑與健康檢查，不計入業務指標
    if path.startswith("/metrics") or path == "/health":
        return await call_next(request)

    start    = time.time()
    response = await call_next(request)
    latency  = time.time() - start

    _req_count.labels(request.method, path, response.status_code).inc()
    _req_latency.labels(request.method, path).observe(latency)
    return response


# ──────────────────────────────────────────────────────────────────────────────
# Route Handlers
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/customer/search", tags=["customer"])
def get_search_customer(
    name: str = Query(default="", description="客戶姓名（模糊查詢，空字串回傳全部）"),
):
    """
    GET /customer/search?name=Alice

    模擬使用者在網頁搜尋框輸入姓名後點「查詢」的情境。
    採 Cache-aside 模式：Redis hit → 直接回傳；miss → 查 DB 並寫回 Redis。
    """
    start      = time.time()
    error_msg  = None
    status_code = 200

    try:
        result = _search_customers_by_name(name)
    except Exception as exc:
        logger.error("GET /customer/search failed: %s", exc)
        error_msg   = str(exc)
        result      = {"error": error_msg}
        status_code = 503

    latency_ms = round((time.time() - start) * 1000, 2)
    _write_request_log(
        endpoint="/customer/search", method="GET",
        func_name="_search_customers_by_name",
        args_summary=f"name={name!r}",
        result_summary=str(result)[:300],
        error_message=error_msg,
        latency_ms=latency_ms, status_code=status_code,
    )
    return JSONResponse(content=result, status_code=status_code)


@app.post("/customer/query", tags=["customer"])
def post_query_customer(body: CustomerQueryBody):
    """
    POST /customer/query

    模擬前端以 POST 傳複合查詢條件（非 insert）。
    例：搜尋頁面有多個篩選欄位（name AND email），選用 POST 而非 GET 是因為
    條件可能複雜、或含敏感資訊不適合放在 URL query string。
    """
    start      = time.time()
    error_msg  = None
    status_code = 200

    try:
        result = _query_customers_compound(body.name, body.email)
    except Exception as exc:
        logger.error("POST /customer/query failed: %s", exc)
        error_msg   = str(exc)
        result      = {"error": error_msg}
        status_code = 503

    latency_ms = round((time.time() - start) * 1000, 2)
    _write_request_log(
        endpoint="/customer/query", method="POST",
        func_name="_query_customers_compound",
        args_summary=body.model_dump_json(),
        result_summary=str(result)[:300],
        error_message=error_msg,
        latency_ms=latency_ms, status_code=status_code,
    )
    return JSONResponse(content=result, status_code=status_code)


@app.post("/customer/register", status_code=201, tags=["customer"])
def post_register_customer(body: CustomerRegisterBody):
    """
    POST /customer/register

    模擬使用者填寫表單並送出，將新客戶資料 INSERT 進 customers 表。
    email 重複時回傳 409 Conflict（不視為系統錯誤）。
    """
    start      = time.time()
    error_msg  = None
    status_code = 201

    try:
        result = _register_new_customer(body.name, body.email, body.phone)
    except Exception as exc:
        error_str = str(exc)
        logger.error("POST /customer/register failed: %s", exc)
        is_duplicate = "unique" in error_str.lower() or "duplicate" in error_str.lower()
        error_msg   = "Email already exists" if is_duplicate else error_str
        result      = {"error": error_msg}
        status_code = 409 if is_duplicate else 503

    latency_ms = round((time.time() - start) * 1000, 2)
    _write_request_log(
        endpoint="/customer/register", method="POST",
        func_name="_register_new_customer",
        args_summary=body.model_dump_json(),
        result_summary=str(result)[:300],
        error_message=error_msg,
        latency_ms=latency_ms, status_code=status_code,
    )
    return JSONResponse(content=result, status_code=status_code)


@app.get("/health", tags=["health"])
def health():
    """快速健康檢查端點。"""
    return {"status": "ok", "service": "user_api", "port": 8001}


# ──────────────────────────────────────────────────────────────────────────────
# Chaos Engineering Endpoints
# 對應主 app 的 /api/anomaly/log-storm 系列，設計概念相同：
#   - 啟動後每筆請求同步寫多筆 log 到 DB + 加入隨機延遲（50~200ms）
#   - 讓 scatter plot 的 Y 軸（latency）顯著上升，P99 遠高於 P50
#   - 可透過 stop 端點即時恢復，無需重啟服務
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/chaos/log-storm/start", tags=["chaos"])
def chaos_log_storm_start():
    """
    啟動 Log Storm 混沌模式。

    效果：每筆業務請求的回應路徑上同步執行：
      - 50~200ms 隨機 sleep（模擬 DB lock contention）
      - 5 筆 user_request_logs INSERT（1 正常 + 4 放大）

    因為 middleware 計時包含整個回應路徑，scatter plot 的 Y 值會顯著上升。

    Grafana 觀察：
      - user_api_log_storm_active → 1
      - Response Time Scatter：P99 從 < 50ms 跳到 200ms+
      - user_api_log_writes_total：rate 快速累積
    """
    _log_storm_event.set()
    _log_storm_active_gauge.set(1)
    logger.warning("[CHAOS] user_api LOG STORM STARTED — 50-200ms delay + 5x log writes per request")
    return {
        "status": "log_storm_started",
        "effect": "Each request now incurs 50-200ms random delay + 5x DB log writes",
        "recovery": "GET /chaos/log-storm/stop",
    }


@app.get("/chaos/log-storm/stop", tags=["chaos"])
def chaos_log_storm_stop():
    """
    停止 Log Storm，立即恢復正常（無需重啟服務）。

    Grafana 觀察：30~60 秒內 scatter plot 的 Y 值應回落到基線水位。
    """
    _log_storm_event.clear()
    _log_storm_active_gauge.set(0)
    logger.info("[CHAOS] user_api LOG STORM STOPPED — returning to normal operation")
    return {
        "status": "log_storm_stopped",
        "message": "Chaos disabled. Monitor Grafana scatter plot for latency recovery.",
    }


@app.get("/chaos/status", tags=["chaos"])
def chaos_status():
    """回傳當前混沌狀態（不修改任何狀態）。"""
    return {
        "user_api_log_storm_active": _log_storm_event.is_set(),
        "prometheus_gauge": int(_log_storm_active_gauge._value.get()),
    }


# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
