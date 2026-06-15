# -*- coding: utf-8 -*-
"""
main.py — 應用程式入口點（App Factory）

職責僅限於：
  1. 建立 FastAPI 實例
  2. lifespan：啟動時初始化 Repository，關閉時釋放資源
  3. 掛載 Middleware（監控）與 /metrics 端點
  4. 組裝各 Router

業務邏輯、DB 操作、快取操作、指標定義皆已分離至各自模組。
"""
import time
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from prometheus_client import make_asgi_app

from config import settings
from metrics import AppMetrics
from repositories.cache_repo import CacheRepository
from repositories.item_repo import ItemRepository
from routers import items as items_router
from routers import anomaly as anomaly_router

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_metrics = AppMetrics()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    應用程式生命週期管理。
    啟動：初始化 Repository 並掛到 app.state，供 Router 透過 DI 取用。
    關閉：釋放 DB 連線池。
    """
    cache_repo = CacheRepository(settings.redis_sentinel_hosts, settings.redis_master_name)
    item_repo = ItemRepository(settings.database_url)

    try:
        cache_repo.ping()
        logger.info("Redis connected successfully")
    except Exception as exc:
        logger.error("Redis connection failed on startup: %s", exc)

    try:
        item_repo.initialize_schema()
    except Exception as exc:
        logger.error("PostgreSQL initialization failed: %s", exc)

    try:
        item_repo.ensure_log_table()
    except Exception as exc:
        logger.error("api_logs table initialization failed: %s", exc)

    app.state.cache_repo = cache_repo
    app.state.item_repo = item_repo

    _metrics.start_system_metrics_collection(interval=5)
    logger.info("System metrics collection started")

    yield  # 應用程式運行中

    item_repo.dispose()


app = FastAPI(lifespan=lifespan, title="API Monitoring — Hybrid Storage")


@app.middleware("http")
async def monitor_requests(request: Request, call_next):
    """攔截所有請求，統一計時並寫入 Prometheus 指標。"""
    start_time = time.time()
    response = await call_next(request)
    process_time = time.time() - start_time
    _metrics.request_count.labels(
        request.method, request.url.path, response.status_code
    ).inc()
    _metrics.request_latency.labels(request.method, request.url.path).observe(
        process_time
    )
    return response


# --- 掛載 Prometheus metrics 端點 ---
app.mount("/metrics", make_asgi_app())

# --- 組裝 Router ---
app.include_router(items_router.router)
app.include_router(anomaly_router.router)


@app.get("/", tags=["health"])
def read_root():
    return {"message": "API is running normally.", "storage": "PostgreSQL + Redis (Hybrid)"}
