# -*- coding: utf-8 -*-
import threading
import logging
import psutil
from prometheus_client import Counter, Histogram, Gauge

logger = logging.getLogger(__name__)


class AppMetrics:
    """
    所有 Prometheus 指標的統一容器。

    採用 Singleton 模式：prometheus_client 不允許同名指標被重複註冊，
    Singleton 確保整個程序生命週期內只初始化一次。

    使用方式：
        metrics = AppMetrics()        # 任何地方呼叫都回傳同一個實例
        metrics.request_count.labels(...).inc()
    """

    _instance: "AppMetrics | None" = None

    def __new__(cls) -> "AppMetrics":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._register_metrics()
        return cls._instance

    def _register_metrics(self) -> None:
        # --- HTTP 層 ---
        self.request_count = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "http_status"],
        )
        self.request_latency = Histogram(
            "http_request_duration_seconds",
            "HTTP request latency in seconds",
            ["method", "endpoint"],
        )

        # --- 快取層（Redis）---
        self.cache_hits = Counter(
            "redis_cache_hits_total",
            "Number of Redis cache hits",
        )
        self.cache_misses = Counter(
            "redis_cache_misses_total",
            "Number of Redis cache misses",
        )
        self.redis_errors = Counter(
            "redis_errors_total",
            "Number of Redis connection or operation errors",
        )

        # --- 資料庫層（PostgreSQL）---
        self.db_errors = Counter(
            "db_errors_total",
            "Number of database errors",
            ["operation"],
        )
        self.db_query_duration = Histogram(
            "db_query_duration_seconds",
            "Database query duration in seconds",
            ["query_type"],
        )

        # --- 系統資源層（Host OS）---
        self.cpu_usage = Gauge(
            "system_cpu_usage_percent",
            "Current CPU usage percentage of the host process",
        )
        self.memory_usage = Gauge(
            "system_memory_usage_percent",
            "Current memory (RAM) usage percentage of the host",
        )
        self.memory_used_bytes = Gauge(
            "system_memory_used_bytes",
            "Current memory (RAM) used in bytes",
        )

        # --- Log Storm 模擬層（同步 DB 寫 Log 反模式）---
        self.log_writes = Counter(
            "log_writes_total",
            "Number of synchronous DB log writes triggered by log storm simulation",
        )
        self.log_write_duration = Histogram(
            "log_write_duration_seconds",
            "Duration of each synchronous DB log write during log storm",
            buckets=[0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0],
        )
        self.log_storm_active = Gauge(
            "log_storm_active",
            "1 if log storm simulation is active, 0 otherwise",
        )
        self.db_active_connections = Gauge(
            "db_active_connections_held",
            "Number of DB connections currently held by connection-exhaust simulation",
        )

        # Log storm 開關旗標（由 /anomaly/log-storm/start|stop 控制）
        self._log_storm_enabled: bool = False

    def start_system_metrics_collection(self, interval: int = 5) -> None:
        """
        啟動一個 Daemon 背景執行緒，每隔 interval 秒用 psutil 採集
        CPU 與 Memory 使用率，並更新對應的 Gauge 指標。

        Daemon 執行緒的特性：主程序結束時自動終止，不需要手動清理。
        由 lifespan 在應用程式啟動時呼叫一次即可。
        """
        def _collect() -> None:
            logger.info("System metrics collector started (interval=%ds)", interval)
            while True:
                try:
                    # cpu_percent(interval=None) 回傳上次呼叫到現在的 CPU 使用率
                    # 第一次呼叫回傳 0.0 屬正常現象
                    self.cpu_usage.set(psutil.cpu_percent(interval=interval))

                    mem = psutil.virtual_memory()
                    self.memory_usage.set(mem.percent)
                    self.memory_used_bytes.set(mem.used)
                except Exception as exc:
                    logger.warning("System metrics collection error: %s", exc)

        thread = threading.Thread(target=_collect, daemon=True, name="system-metrics")
        thread.start()
