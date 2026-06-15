# -*- coding: utf-8 -*-
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """
    應用程式設定，全部從環境變數讀取。
    frozen=True 確保設定在執行期間不可被修改（immutable）。
    使用 from_env() 工廠方法建立，方便在測試時傳入自訂值。
    """
    redis_sentinel_hosts: str  # 逗號分隔："sentinel1:26379,sentinel2:26379,sentinel3:26379"
    redis_master_name: str     # Sentinel 監控的主識別名，預設 "mymaster"
    database_url: str
    cache_ttl: int  # Redis key 的存活秒數

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            redis_sentinel_hosts=os.getenv(
                "REDIS_SENTINEL_HOSTS",
                "localhost:26379,localhost:26380,localhost:26381",
            ),
            redis_master_name=os.getenv("REDIS_MASTER_NAME", "mymaster"),
            database_url=os.getenv(
                "DATABASE_URL",
                "postgresql://appuser:apppassword@localhost:5432/appdb",
            ),
            cache_ttl=int(os.getenv("CACHE_TTL", "10")),
        )


# 模組層級的單例，供整個應用程式直接 import 使用
settings = Settings.from_env()
