# -*- coding: utf-8 -*-
import time
import random
import logging
import threading

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from metrics import AppMetrics

logger = logging.getLogger(__name__)


class ItemRepository:
    """
    封裝所有對 PostgreSQL items 資料表的操作。

    職責：
      - 管理 SQLAlchemy 連線池
      - 建立 Schema 並填入初始測試資料
      - 實作 get_random_item / insert_item 等 CRUD 操作
      - 自行追蹤 DB 查詢耗時與錯誤指標
      - 提供混沌工程用的 execute_slow_query()

    呼叫端（Router）只需處理業務邏輯，無需關心 SQL 細節或指標更新。
    """

    def __init__(self, database_url: str) -> None:
        self._engine: Engine = create_engine(
            database_url,
            pool_pre_ping=True,  # 每次取連線前先 ping，自動處理斷線重連
            pool_size=20,
        )
        self._metrics = AppMetrics()

    def initialize_schema(self) -> None:
        """
        建立 items 資料表（若不存在）並填入 100 筆種子資料。
        由 lifespan 在應用程式啟動時呼叫一次。
        """
        with self._engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS items (
                    id         SERIAL PRIMARY KEY,
                    name       VARCHAR(100) NOT NULL,
                    value      FLOAT        NOT NULL,
                    created_at TIMESTAMP    DEFAULT NOW()
                )
            """))
            count = conn.execute(text("SELECT COUNT(*) FROM items")).scalar()
            if count == 0:
                for i in range(100):
                    conn.execute(
                        text("INSERT INTO items (name, value) VALUES (:name, :value)"),
                        {"name": f"item_{i}", "value": round(random.uniform(1.0, 1000.0), 2)},
                    )
            conn.commit()
        logger.info("PostgreSQL schema initialized")

    def get_random_item(self) -> dict:
        """
        隨機取一筆資料。
        成功 → 回傳 dict 並記錄查詢耗時
        失敗 → 增加 db_errors{operation="select"} 計數並重新拋出
        """
        start = time.time()
        try:
            with self._engine.connect() as conn:
                row = conn.execute(
                    text("SELECT name, value FROM items ORDER BY RANDOM() LIMIT 1")
                ).fetchone()
        except Exception as exc:
            self._metrics.db_errors.labels(operation="select").inc()
            logger.error("DB select failed: %s", exc)
            raise
        self._metrics.db_query_duration.labels(query_type="select").observe(
            time.time() - start
        )
        return {"name": row[0], "value": row[1]} if row else {}

    def insert_item(self, name: str, value: float) -> float:
        """
        新增一筆資料，回傳實際耗時（秒）。
        失敗 → 增加 db_errors{operation="insert"} 計數並重新拋出
        """
        start = time.time()
        try:
            with self._engine.connect() as conn:
                conn.execute(
                    text("INSERT INTO items (name, value) VALUES (:name, :value)"),
                    {"name": name, "value": value},
                )
                conn.commit()
        except Exception as exc:
            self._metrics.db_errors.labels(operation="insert").inc()
            logger.error("DB insert failed: %s", exc)
            raise
        duration = time.time() - start
        self._metrics.db_query_duration.labels(query_type="insert").observe(duration)
        return duration

    def execute_slow_query(self) -> float:
        """
        混沌工程：執行 pg_sleep(3) 模擬慢查詢，回傳實際耗時（秒）。
        觀察指標：db_query_duration_seconds{query_type="slow_query"} P99 飆升
        """
        start = time.time()
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT pg_sleep(3)"))
        except Exception as exc:
            self._metrics.db_errors.labels(operation="slow_query").inc()
            logger.error("Slow query execution failed: %s", exc)
            raise
        duration = time.time() - start
        self._metrics.db_query_duration.labels(query_type="slow_query").observe(duration)
        return duration

    def dispose(self) -> None:
        """釋放連線池，由 lifespan 在應用程式關閉時呼叫。"""
        self._engine.dispose()

    # ------------------------------------------------------------------ #
    #  以下為 Chaos Engineering / 模擬場景用方法                           #
    # ------------------------------------------------------------------ #

    def ensure_log_table(self) -> None:
        """
        建立 api_logs 資料表（若不存在）。
        這個表模擬「記錄 API 請求 log 至 DB」的反模式設計。
        由 lifespan 在啟動時呼叫一次。
        """
        with self._engine.connect() as conn:
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS api_logs (
                    id          SERIAL PRIMARY KEY,
                    endpoint    VARCHAR(200) NOT NULL,
                    status_code INTEGER      NOT NULL,
                    latency_ms  FLOAT        NOT NULL,
                    logged_at   TIMESTAMP    DEFAULT NOW()
                )
            """))
            conn.commit()
        logger.info("api_logs table ensured")

    def write_log_to_db(self, endpoint: str, status_code: int, latency_ms: float) -> float:
        """
        模擬「同步把 request log 寫進 DB」的反模式行為。

        真實世界問題：
          - 每個 API 請求都需要額外一次 DB 連線與 INSERT
          - 加入 50~150ms 的 jitter 模擬實際資料庫寫入延遲與鎖競爭
          - 高流量下連線池（pool_size=5）迅速耗盡，後續請求開始等待
          - 等待 → latency 上升 → 請求堆積 → 惡性循環

        觀察指標：
          - log_writes_total：每秒寫 log 次數
          - log_write_duration_seconds：寫入延遲（P99 可觀察惡化趨勢）
          - http_request_duration_seconds：整體請求延遲被拉高
        """
        start = time.time()
        try:
            with self._engine.connect() as conn:
                # 模擬真實 DB 寫入延遲：contention + I/O jitter
                time.sleep(random.uniform(0.05, 0.15))
                conn.execute(
                    text(
                        "INSERT INTO api_logs (endpoint, status_code, latency_ms) "
                        "VALUES (:endpoint, :status_code, :latency_ms)"
                    ),
                    {"endpoint": endpoint, "status_code": status_code, "latency_ms": latency_ms},
                )
                conn.commit()
        except Exception as exc:
            self._metrics.db_errors.labels(operation="log_write").inc()
            logger.error("Synchronous log write to DB failed: %s", exc)
            raise
        duration = time.time() - start
        self._metrics.log_write_duration.observe(duration)
        self._metrics.log_writes.inc()
        return duration

    def exhaust_connections(self, hold_count: int = 4, hold_seconds: float = 10.0) -> None:
        """
        混沌工程：同時佔住 hold_count 條 DB 連線，持續 hold_seconds 秒後自動釋放。

        pool_size=20，佔住 18 條後只剩 2 條可用，其他請求被迫等待，
        延遲 P99 飆升，Grafana 上可清楚看到。
        10 秒後自動釋放，系統自動恢復，示範「可回溯性」。

        觀察指標：
          - db_active_connections_held：持有連線數
          - http_request_duration_seconds：等待期間 P99 明顯上升
        """
        self._metrics.db_active_connections.set(hold_count)
        logger.warning(
            "Connection exhaust started: holding %d connections for %.0fs",
            hold_count, hold_seconds
        )

        def _hold_one():
            try:
                with self._engine.connect() as conn:
                    conn.execute(text(f"SELECT pg_sleep({hold_seconds})"))
            except Exception:
                pass

        threads = [
            threading.Thread(target=_hold_one, daemon=True, name=f"exhaust-{i}")
            for i in range(hold_count)
        ]
        for t in threads:
            t.start()

        def _reset_gauge():
            time.sleep(hold_seconds + 1)
            self._metrics.db_active_connections.set(0)
            logger.info("Connection exhaust ended: connections released")

        threading.Thread(target=_reset_gauge, daemon=True, name="exhaust-reset").start()

    def force_db_error(self) -> None:
        """
        強制觸發一個 DB 錯誤（查詢不存在的資料表）。
        讓 db_errors_total{operation="forced_error"} 計數增加。
        """
        try:
            with self._engine.connect() as conn:
                conn.execute(text("SELECT * FROM _chaos_nonexistent_table_"))
        except Exception as exc:
            self._metrics.db_errors.labels(operation="forced_error").inc()
            logger.error("Forced DB error triggered: %s", exc)
            raise
