import logging
import os
import time
import traceback
from functools import wraps

# python-dotenv 為選配：有裝就載入 .env，沒裝不影響使用
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ──────────────────────────────────────────────
#  工具函式
# ──────────────────────────────────────────────

def find_project_root(marker: str = "api_monitoring") -> str:
    """
    從 logger_manager.py 所在位置往上爬，找到包含 marker 的路徑段後回傳。
    找不到時回傳 logger_manager.py 同層目錄（安全降級）。
    """
    current = os.path.abspath(__file__)
    while current and current != os.path.dirname(current):
        if marker in os.path.basename(current):
            return current + os.sep
        parent = os.path.dirname(current)
        # 找 parent 目錄中是否有 marker 子目錄
        if os.path.isdir(os.path.join(parent, marker)):
            return os.path.join(parent, marker) + os.sep
        current = parent
    # 找不到 marker → 用 logger_manager.py 所在目錄
    return os.path.dirname(os.path.abspath(__file__)) + os.sep


# ──────────────────────────────────────────────
#  setup_logging
# ──────────────────────────────────────────────

def setup_logging(logger_name: str, level: str | None = None) -> logging.Logger:
    """
    初始化指定名稱的 logger。

    - 同時輸出到 logs/<logger_name>.log 檔案與 console（terminal）
    - log level 優先順序：參數 level > 環境變數 LOG_LEVEL > 預設 DEBUG
    - 重複呼叫同名 logger 不會重複加 handler

    Args:
        logger_name: logger 識別名稱，也作為 .log 檔案名稱
        level:       可選，明確指定 level（'DEBUG'/'INFO'/'WARNING'/'ERROR'）

    Returns:
        已設定好的 logging.Logger
    """
    project_root = find_project_root()
    logs_dir = os.path.join(project_root, "logs")
    os.makedirs(logs_dir, exist_ok=True)

    logger = logging.getLogger(logger_name)

    # 避免重複 addHandler（若已設定過就直接回傳）
    if logger.handlers:
        return logger

    raw_level = level or os.getenv("LOG_LEVEL", "DEBUG")
    log_level = getattr(logging, raw_level.upper(), logging.DEBUG)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 檔案 handler
    file_handler = logging.FileHandler(
        os.path.join(logs_dir, f"{logger_name}.log"),
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # Console handler（讓 terminal 也看得到）
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.info("Logger '%s' initialized → %s", logger_name, logs_dir)
    return logger


# ──────────────────────────────────────────────
#  log_decorator
# ──────────────────────────────────────────────

def log_decorator(func):
    """
    通用 log 裝飾器，記錄：
      - 呼叫的 module / function 名稱
      - 傳入的 args & kwargs（縮短至 200 字元避免爆 log）
      - 執行耗時（ms）
      - 回傳值（縮短至 200 字元）
      - 若 raise exception：完整 traceback

    使用方式：
        @log_decorator
        def my_function(x, y):
            return x + y

    注意：裝飾器使用 logging.getLogger(func.__name__)，
    需先呼叫 setup_logging(func.__name__) 或以其他 logger 設定過才能看到 .log 檔。
    若沒設定也不會報錯，只是 log 不會寫到檔案（僅 propagate 給 root logger）。
    """
    @wraps(func)
    def wrapper(*args, **kwargs):
        local_logger = logging.getLogger(func.__name__)
        divider = "─" * 60

        args_repr = repr(args)[:200]
        kwargs_repr = repr(kwargs)[:200]

        local_logger.debug("%s", divider)
        local_logger.debug(
            "[%s.%s] START | args=%s | kwargs=%s",
            func.__module__, func.__name__, args_repr, kwargs_repr,
        )

        t0 = time.perf_counter()
        try:
            result = func(*args, **kwargs)
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            result_repr = repr(result)[:200]
            local_logger.info(
                "[%s.%s] OK | elapsed=%sms | return=%s",
                func.__module__, func.__name__, elapsed_ms, result_repr,
            )
            return result
        except Exception:
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            tb = traceback.format_exc()
            local_logger.error(
                "[%s.%s] ERROR | elapsed=%sms\n%s",
                func.__module__, func.__name__, elapsed_ms, tb,
            )
            raise

    return wrapper