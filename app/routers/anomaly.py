# -*- coding: utf-8 -*-
import time
import logging

from fastapi import APIRouter, Request, Response

from metrics import AppMetrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/anomaly", tags=["anomaly / chaos"])


@router.get("/lag")
def simulate_lag():
    """
    泛型延遲模擬（sleep 2.5s）。
    觀察指標：http_request_duration_seconds P99 升高
    """
    time.sleep(2.5)
    return {"status": "slow", "message": "Simulating generic system lag"}


@router.get("/error")
def simulate_error():
    """
    直接回傳 HTTP 500。
    觀察指標：http_requests_total{http_status="500"} 增加
    """
    return Response(content="Internal Server Error", status_code=500)


@router.get("/db-overload")
def simulate_db_overload(request: Request):
    """
    對 PostgreSQL 發出 pg_sleep(3) 模擬慢查詢。
    觀察指標：db_query_duration_seconds{query_type="slow_query"} P99 飆升
    """
    item_repo = request.app.state.item_repo
    try:
        duration = item_repo.execute_slow_query()
        return {"status": "slow_query_done", "duration_seconds": round(duration, 2)}
    except Exception as exc:
        logger.error("db-overload simulation failed: %s", exc)
        return Response(
            content='{"error": "DB overload failed"}',
            status_code=503,
            media_type="application/json",
        )


@router.get("/cache-flush")
def simulate_cache_flush(request: Request):
    """
    清空 Redis 整個 DB，強制下一批 /api/data 請求全部打到 PostgreSQL。
    觀察指標：redis_cache_misses_total 暴增，命中率驟降後緩慢回升（TTL 重建）
    """
    cache_repo = request.app.state.cache_repo
    try:
        cache_repo.flush()
        return {
            "status": "cache_flushed",
            "message": "Redis cache cleared. Next /api/data calls will hit PostgreSQL.",
        }
    except Exception as exc:
        logger.error("cache-flush simulation failed: %s", exc)
        return Response(
            content='{"error": "Cache flush failed"}',
            status_code=503,
            media_type="application/json",
        )


@router.get("/redis-down")
def simulate_redis_down(request: Request):
    """
    嘗試連接一個不存在的 Redis 節點，模擬 Redis 斷線。
    觀察指標：redis_errors_total 持續累加
    """
    cache_repo = request.app.state.cache_repo
    try:
        cache_repo.simulate_connection_failure()
    except Exception as exc:
        return Response(
            content=f'{{"error": "Redis connection failed", "type": "{type(exc).__name__}"}}',
            status_code=503,
            media_type="application/json",
        )
    return {"status": "unexpected_success"}


# ------------------------------------------------------------------ #
#  以下為新增的 Chaos Engineering endpoints                            #
# ------------------------------------------------------------------ #

@router.get("/db-error")
def simulate_db_error(request: Request):
    """
    強制觸發一個 DB 查詢錯誤（查詢不存在的資料表）。

    觀察指標：db_errors_total{operation="forced_error"} 計數增加
    """
    item_repo = request.app.state.item_repo
    try:
        item_repo.force_db_error()
    except Exception as exc:
        return Response(
            content=f'{{"error": "DB error triggered", "type": "{type(exc).__name__}"}}',
            status_code=503,
            media_type="application/json",
        )
    return {"status": "unexpected_success"}


@router.get("/log-storm/start")
def start_log_storm():
    """
    啟動「Log Storm」模擬：模擬上線了「同步將 request log 寫入 DB」的功能。

    啟動後每個 /api/data 請求都會同步執行一次 DB INSERT（api_logs 表）。
    加上 50~150ms 的寫入 jitter 模擬 DB contention。
    在高流量下 pool_size=5 的連線池會迅速耗盡，導致：
      1. 請求 latency 爬升
      2. 請求開始 timeout / 503
      3. 系統陷入惡性循環

    恢復方法：呼叫 /api/anomaly/log-storm/stop（立即生效，無需重啟）

    觀察指標：
      - log_storm_active（Gauge）變為 1
      - log_writes_total（Counter）開始累積
      - log_write_duration_seconds P99 可觀察寫入延遲
      - http_request_duration_seconds P99 整體請求延遲爬升
    """
    metrics = AppMetrics()
    metrics._log_storm_enabled = True
    metrics.log_storm_active.set(1)
    logger.warning("LOG STORM STARTED — synchronous DB logging is now active on all /api/data requests")
    return {
        "status": "log_storm_started",
        "effect": "Every /api/data request now synchronously writes to api_logs table (50-150ms per write)",
        "recovery": "Call GET /api/anomaly/log-storm/stop to restore normal operation immediately",
    }


@router.get("/log-storm/stop")
def stop_log_storm():
    """
    停止 Log Storm，立即恢復正常。無需重啟容器。

    觀察指標：
      - log_storm_active（Gauge）回到 0
      - http_request_duration_seconds P99 應在 30 秒內恢復正常水位
    """
    metrics = AppMetrics()
    metrics._log_storm_enabled = False
    metrics.log_storm_active.set(0)
    logger.info("LOG STORM STOPPED — synchronous DB logging disabled, system returning to normal")
    return {
        "status": "log_storm_stopped",
        "message": "Synchronous DB logging disabled. Monitor Grafana for latency recovery.",
    }


@router.get("/log-storm/status")
def log_storm_status():
    """回傳 Log Storm 目前狀態（不修改任何狀態）。"""
    metrics = AppMetrics()
    return {
        "log_storm_active": metrics._log_storm_enabled,
        "prometheus_gauge": int(metrics.log_storm_active._value.get()),
    }


@router.get("/connection-exhaust")
def simulate_connection_exhaust(request: Request):
    """
    混沌工程：佔住 18 條 DB 連線持續 30 秒，連線池只剩 2 條可用。

    模擬場景：DB 連線池耗盡（因 log storm 或其他高連線負載），
    其餘請求被迫等待連線，latency P99 飆升。
    30 秒後自動釋放連線，系統自動恢復。

    觀察指標：
      - db_active_connections_held（Gauge）升至 18
      - http_request_duration_seconds P99 明顯上升（等待連線）
      - 30 秒後 Gauge 歸零，latency 自動恢復
    """
    item_repo = request.app.state.item_repo
    item_repo.exhaust_connections(hold_count=18, hold_seconds=30.0)
    return {
        "status": "connection_exhaust_started",
        "held_connections": 18,
        "pool_size": 20,
        "remaining_connections": 2,
        "auto_release_seconds": 30,
        "warning": "DB connection pool is 90% occupied for 30 seconds. Expect latency spikes.",
    }
