# -*- coding: utf-8 -*-
import random
import logging

from fastapi import APIRouter, Request, Response

from config import settings
from metrics import AppMetrics

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["items"])

_metrics = AppMetrics()
_CACHE_KEYS_POOL = 10  # 模擬 10 種不同資源，讓 Cold Start / Warm-up / Miss 都清楚可見


@router.get("/data")
def get_data(request: Request):
    """
    Cache-aside 讀取模式：
      1. 先查 Redis（hit → 直接回傳，耗時 < 1ms）
      2. Miss → 查 PostgreSQL，把結果寫回 Redis（TTL 由 settings.cache_ttl 控制）
      3. Redis 或 DB 任一發生錯誤 → 回傳 503

    Log Storm 模式啟動時，請求結束後會額外同步寫一筆 log 至 api_logs 表，
    模擬上線了記錄 API log 至 DB 功能後對連線池的壓力。

    觀察指標：
      - redis_cache_hits_total vs redis_cache_misses_total（命中率）
      - db_query_duration_seconds{query_type="select"}
      - log_write_duration_seconds（log storm 啟動時）
    """
    cache_repo = request.app.state.cache_repo
    item_repo = request.app.state.item_repo

    import time as _time
    req_start = _time.time()

    # 每次請求隨機選一個 key，模擬不同 user/resource 的快取查詢
    cache_key = f"data:{random.randint(1, _CACHE_KEYS_POOL)}"

    cached = cache_repo.get(cache_key)
    if cached is not None:
        result_source = "cache (Redis)"
        result_data = cached
    else:
        try:
            result = item_repo.get_random_item()
            cache_repo.setex(cache_key, settings.cache_ttl, str(result))
            result_source = "database (PostgreSQL)"
            result_data = result
        except Exception as exc:
            logger.error("get_data failed after cache miss: %s", exc)
            return Response(
                content='{"error": "Database unavailable"}',
                status_code=503,
                media_type="application/json",
            )

    # Log Storm 攔截：同步寫 log 至 DB（模擬反模式設計）
    if _metrics._log_storm_enabled:
        latency_ms = round((_time.time() - req_start) * 1000, 2)
        try:
            item_repo.write_log_to_db(
                endpoint="/api/data",
                status_code=200,
                latency_ms=latency_ms,
            )
        except Exception as exc:
            logger.error("Log storm DB write failed: %s", exc)

    return {"source": result_source, "data": result_data}


@router.get("/write")
def write_data(request: Request):
    """
    寫入一筆隨機資料到 PostgreSQL。

    觀察指標：
      - db_query_duration_seconds{query_type="insert"}
      - db_errors_total{operation="insert"}
    """
    item_repo = request.app.state.item_repo
    name = f"item_{random.randint(1000, 9999)}"
    value = round(random.uniform(1.0, 1000.0), 2)

    try:
        duration = item_repo.insert_item(name, value)
        return {
            "status": "written",
            "name": name,
            "value": value,
            "db_latency_ms": round(duration * 1000, 1),
        }
    except Exception as exc:
        logger.error("write_data failed: %s", exc)
        return Response(
            content='{"error": "Database write error"}',
            status_code=503,
            media_type="application/json",
        )
