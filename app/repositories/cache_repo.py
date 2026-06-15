# -*- coding: utf-8 -*-
import logging
from typing import Optional

import redis as redis_lib
from redis.sentinel import Sentinel

from metrics import AppMetrics

logger = logging.getLogger(__name__)


class CacheRepository:
    """
    封裝所有對 Redis 的操作。

    職責：
      - 透過 Redis Sentinel 取得當前 Master 連線（自動 Failover）
      - 實作 get / setex / flush 等快取操作
      - 自行追蹤 cache hit/miss 與 Redis 錯誤指標
      - 提供混沌工程用的 simulate_connection_failure()

    Sentinel HA 流程：
      sentinel_hosts 中的任一 Sentinel 回傳當前 Master 位址
      → redis-py 直接連線 Master 執行操作
      → Master 故障後 Sentinel 完成 Failover，下次操作自動取得新 Master
    """

    def __init__(self, sentinel_hosts: str, master_name: str) -> None:
        hosts = [
            (h.split(":")[0], int(h.split(":")[1]))
            for h in sentinel_hosts.split(",")
        ]
        _sentinel = Sentinel(
            hosts,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        # 所有讀寫操作都走 Master（保證 Cache-Aside 的一致性）
        self._client = _sentinel.master_for(master_name, decode_responses=True)
        self._metrics = AppMetrics()

    def ping(self) -> None:
        """測試連線是否正常，供 lifespan 啟動檢查使用。"""
        self._client.ping()

    def get(self, key: str) -> Optional[str]:
        """
        取得 key 的快取值。
        - Hit  → 回傳字串，並增加 cache_hits 計數
        - Miss → 回傳 None，並增加 cache_misses 計數
        - 錯誤 → 回傳 None，增加 redis_errors 與 cache_misses（退回至 DB）
        """
        try:
            value = self._client.get(key)
            if value is not None:
                self._metrics.cache_hits.inc()
            else:
                self._metrics.cache_misses.inc()
            return value
        except Exception as exc:
            self._metrics.redis_errors.inc()
            self._metrics.cache_misses.inc()
            logger.warning("Redis read error: %s", exc)
            return None

    def setex(self, key: str, ttl: int, value: str) -> None:
        """寫入快取並設定 TTL（秒）。失敗時靜默降級，不影響主流程。"""
        try:
            self._client.setex(key, ttl, value)
        except Exception as exc:
            self._metrics.redis_errors.inc()
            logger.warning("Redis write error: %s", exc)

    def flush(self) -> None:
        """
        清空當前 DB 所有 key。
        失敗時增加 redis_errors 計數並重新拋出，由呼叫端決定 HTTP 回應。
        """
        try:
            self._client.flushdb()
        except Exception as exc:
            self._metrics.redis_errors.inc()
            logger.error("Redis flush failed: %s", exc)
            raise

    def simulate_connection_failure(self) -> None:
        """
        混沌工程：嘗試連接一個不存在的 Redis 節點。
        預期一定會失敗，失敗時增加 redis_errors 計數並重新拋出。
        觀察指標：redis_errors_total 持續累加
        """
        dead_client = redis_lib.Redis(
            host="non-existent-redis",
            port=6379,
            socket_connect_timeout=1,
        )
        try:
            dead_client.ping()
        except Exception as exc:
            self._metrics.redis_errors.inc()
            logger.error("[Chaos] Simulated Redis down: %s", type(exc).__name__)
            raise
