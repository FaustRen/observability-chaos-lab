#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
user_traffic_generator.py — 模擬真實使用者流量並整合混沌功能

架構設計：
  同時打兩個服務，對應 Grafana 四層觀測鏈：

  user_api.py  (port 8001) ← 業務流量 → scatter plot 動起來
  main app     (port 8000) ← 同時也打 → Redis Cache / DB / Log Storm 面板動起來

混沌劇本（互動式）：
  Phase 1  正常基線        → 兩個服務都有穩定低延遲流量
  Phase 2  user_api 慢下來 → 呼叫 /chaos/log-storm/start，scatter P99 跳升
  Phase 3  全系統 Log Storm → 呼叫主 app /api/anomaly/log-storm/start，Redis/DB 面板劇變
  Phase 4  連線池耗盡      → 呼叫主 app /api/anomaly/connection-exhaust，DB panel 出現高峰
  Phase 5  恢復            → 全部停止，觀察各面板回落

使用方式：
  python user_traffic_generator.py                    # 持續正常流量
  python user_traffic_generator.py --rps 5            # 指定 RPS
  python user_traffic_generator.py --interactive      # 有暫停點的互動示範（簡報用）
  python user_traffic_generator.py --chaos log-storm  # 直接以混沌模式啟動
"""

import argparse
import random
import string
import sys
import threading
import time
from collections import deque

import requests

USER_API_URL   = "http://localhost:8001"
MAIN_API_URL   = "http://localhost:8000"
PROMETHEUS_URL = "http://localhost:9090"
SEPARATOR      = "=" * 65


# ──────────────────────────────────────────────────────────────────────────────
# 統計收集器（執行緒安全）
# ──────────────────────────────────────────────────────────────────────────────

class Stats:
    def __init__(self, name: str):
        self.name      = name
        self._lock     = threading.Lock()
        self.total     = 0
        self.success   = 0
        self.errors    = 0
        self.latencies = deque(maxlen=1000)

    def record(self, latency_ms: float, ok: bool):
        with self._lock:
            self.total += 1
            self.latencies.append(latency_ms)
            if ok:
                self.success += 1
            else:
                self.errors += 1

    def snapshot(self) -> dict:
        with self._lock:
            lats = sorted(self.latencies)
            n    = len(lats)
            return {
                "total":   self.total,
                "success": self.success,
                "errors":  self.errors,
                "avg_ms":  round(sum(lats) / n, 1) if n else 0,
                "p50_ms":  round(lats[int(n * 0.50)], 1) if n >= 2 else 0,
                "p99_ms":  round(lats[int(n * 0.99) - 1], 1) if n >= 10 else 0,
            }


_stats_user = Stats("user_api")
_stats_main = Stats("main_app")


# ──────────────────────────────────────────────────────────────────────────────
# Tee — 同時輸出到 console 和 log 檔（不需改動任何 print）
# ──────────────────────────────────────────────────────────────────────────────

class _Tee:
    """將 sys.stdout 的所有寫入同步鏡像到 log 檔。"""
    def __init__(self, console, log_fh):
        self._console = console
        self._log_fh  = log_fh

    def write(self, data: str):
        self._console.write(data)
        self._log_fh.write(data)

    def flush(self):
        self._console.flush()
        self._log_fh.flush()

    def fileno(self):
        return self._console.fileno()


# ──────────────────────────────────────────────────────────────────────────────
# HTTP 呼叫工具
# ──────────────────────────────────────────────────────────────────────────────

def _call(base_url: str, method: str, path: str,
          json_body: dict | None = None, stats: Stats | None = None) -> dict | None:
    try:
        t0   = time.time()
        resp = requests.request(method, f"{base_url}{path}", json=json_body, timeout=12)
        lat  = (time.time() - t0) * 1000
        ok   = resp.status_code < 500
        if stats:
            stats.record(lat, ok)
        return resp.json() if "application/json" in resp.headers.get("content-type", "") else {}
    except requests.exceptions.RequestException as exc:
        if stats:
            stats.record(9999, False)
        return None


# ──────────────────────────────────────────────────────────────────────────────
# 隨機資料產生器
# ──────────────────────────────────────────────────────────────────────────────

_FIRST = ["Alice", "Bob", "Carol", "David", "Eva", "Frank", "Grace",
          "Henry", "Irene", "Jack", "Karen", "Leo", "Mary", "Nick",
          "Olivia", "Paul", "Quinn", "Rose", "Sam", "Tina"]
_LAST  = ["Wang", "Chen", "Lin", "Huang", "Chang", "Wu", "Liu",
          "Yang", "Hsu", "Cheng", "Tsai", "Liao", "Hsieh", "Chou"]

def _rnd_name()  -> str: return f"{random.choice(_FIRST)} {random.choice(_LAST)}"
def _rnd_email() -> str: return f"user_{''.join(random.choices(string.ascii_lowercase+string.digits, k=7))}@loadtest.local"
def _rnd_phone() -> str: return f"09{random.randint(10,99)}-{random.randint(100,999)}-{random.randint(100,999)}"
def _rnd_search()-> str: return random.choice(_FIRST[:12])  # 前 12 個 → 產生 cache hit 混合


# ──────────────────────────────────────────────────────────────────────────────
# user_api.py 請求動作
# ──────────────────────────────────────────────────────────────────────────────

def _user_search():
    _call(USER_API_URL, "GET", f"/customer/search?name={_rnd_search()}", stats=_stats_user)

def _user_query():
    body = {"name": random.choice(["", random.choice(_FIRST)]), "email": ""}
    _call(USER_API_URL, "POST", "/customer/query", json_body=body, stats=_stats_user)

def _user_register():
    body = {"name": _rnd_name(), "email": _rnd_email(), "phone": _rnd_phone()}
    _call(USER_API_URL, "POST", "/customer/register", json_body=body, stats=_stats_user)

# 正常流量比例：讀多寫少（符合真實 Cache-aside 情境）
_USER_ACTIONS_NORMAL = (
    [_user_search] * 60 + [_user_query] * 30 + [_user_register] * 10
)

# ──────────────────────────────────────────────────────────────────────────────
# main app (port 8000) 請求動作 — 讓 Redis/DB Grafana 面板有資料
# ──────────────────────────────────────────────────────────────────────────────

def _main_data():
    _call(MAIN_API_URL, "GET", "/api/data", stats=_stats_main)

def _main_write():
    _call(MAIN_API_URL, "GET", "/api/write", stats=_stats_main)

_MAIN_ACTIONS = [_main_data] * 7 + [_main_write] * 3


# ──────────────────────────────────────────────────────────────────────────────
# Chaos 控制函式
# ──────────────────────────────────────────────────────────────────────────────

def _chaos_user_start():
    r = _call(USER_API_URL, "GET", "/chaos/log-storm/start")
    active = r.get("status") if r else "failed"
    print(f"  [CHAOS] user_api log storm → {active}")

def _chaos_user_stop():
    r = _call(USER_API_URL, "GET", "/chaos/log-storm/stop")
    status = r.get("status") if r else "failed"
    print(f"  [RECOVERY] user_api log storm → {status}")

def _chaos_main_start():
    r = _call(MAIN_API_URL, "GET", "/api/anomaly/log-storm/start")
    status = r.get("status") if r else "failed"
    print(f"  [CHAOS] main app log storm → {status}")

def _chaos_main_stop():
    r = _call(MAIN_API_URL, "GET", "/api/anomaly/log-storm/stop")
    status = r.get("status") if r else "failed"
    print(f"  [RECOVERY] main app log storm → {status}")

def _chaos_connection_exhaust():
    r = _call(MAIN_API_URL, "GET", "/api/anomaly/connection-exhaust")
    status = r.get("status") if r else "failed"
    print(f"  [CHAOS] connection exhaust → {status} (auto-releases in 10s)")


def _chaos_get_primary() -> dict | None:
    """
    查詢三個 Patroni REST API（port 8081/8082/8083），找出當前 Primary 節點。
    回傳 {"name": "patroni1", "rest_port": 8081} 或 None（找不到）。
    """
    for port, name in [(8081, "patroni1"), (8082, "patroni2"), (8083, "patroni3")]:
        try:
            r = requests.get(f"http://localhost:{port}/patroni", timeout=3)
            if r.status_code == 200:
                info = r.json()
                if info.get("role") in ("master", "primary"):
                    return {"name": name, "rest_port": port}
        except Exception:
            pass
    return None


def _chaos_failover_primary():
    """
    Phase 6 HA Failover 示範：
      1. 透過 Patroni REST API 找出當前 Primary
      2. 以 docker stop 強制停止該容器（模擬機器猝死）
      3. etcd TTL 到期後 Patroni 觸發 Failover，新 Primary 升格
      4. HAProxy 在下一個 check 週期自動切換路由（約 3~9 秒）
      5. 觀察流量錯誤後恢復，驗證 RTO（Recovery Time Objective）
      6. docker start 重啟舊 Primary，它以 Replica 身份重新加入
    回傳舊 Primary 的容器名稱（供後續重啟使用）。
    """
    import subprocess

    primary = _chaos_get_primary()
    if not primary:
        print("  [CHAOS] 無法連接 Patroni REST API（確認叢集是否已啟動）")
        return None

    primary_name = primary["name"]
    # docker-compose 容器命名規則：{專案目錄名}-{服務名}-{編號}
    project = "api_monitoring"
    container = f"{project}-{primary_name}-1"
    print(f"  [CHAOS] 當前 Primary：{primary_name}（Patroni REST: localhost:{primary['rest_port']}）")
    print(f"  [CHAOS] 停止容器：{container}  ← 模擬機器猝死 (SIGTERM)")

    result = subprocess.run(
        ["docker", "stop", container],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        print(f"  [CHAOS] 容器 {container} 已停止 ✓")
    else:
        print(f"  [CHAOS] 停止失敗：{result.stderr.strip()}")
        return None

    return container


def _chaos_restart_container(container: str):
    """重啟已停止的容器，使其以 Replica 身份重新加入叢集。"""
    import subprocess
    print(f"  [RECOVERY] 重啟容器：{container}（將以 Replica 身份加入叢集）")
    result = subprocess.run(
        ["docker", "start", container],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode == 0:
        print(f"  [RECOVERY] 容器 {container} 已重啟 ✓")
    else:
        print(f"  [RECOVERY] 重啟失敗：{result.stderr.strip()}")


def _chaos_trigger_db_errors(count: int = 3, interval_s: float = 3.0):
    """
    強制觸發幾次 DB 錯誤（查詢不存在的資料表），讓 db_errors 面板有可見資料。
    這會增加 db_errors_total{operation="forced_error"} 計數器。
    """
    for i in range(count):
        r = _call(MAIN_API_URL, "GET", "/api/anomaly/db-error")
        # 404/500 都算有收到回應，r 可能是 None（連線失敗）或 {} 或 {"error": ...}
        triggered = "ok" if r is not None else "no-response"
        print(f"  [CHAOS] forced DB error #{i+1}/{count} → {triggered}")
        if i < count - 1:
            time.sleep(interval_s)


# ──────────────────────────────────────────────────────────────────────────────
# 流量工作執行緒
# ──────────────────────────────────────────────────────────────────────────────

def _user_worker(rps: float, stop: threading.Event):
    interval = 1.0 / max(rps, 0.1)
    while not stop.is_set():
        t0 = time.time()
        random.choice(_USER_ACTIONS_NORMAL)()
        remaining = interval - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)

def _main_worker(rps: float, stop: threading.Event):
    interval = 1.0 / max(rps, 0.1)
    while not stop.is_set():
        t0 = time.time()
        random.choice(_MAIN_ACTIONS)()
        remaining = interval - (time.time() - t0)
        if remaining > 0:
            time.sleep(remaining)


# ──────────────────────────────────────────────────────────────────────────────
# 統計列印執行緒
# ──────────────────────────────────────────────────────────────────────────────

def _stats_printer(stop: threading.Event):
    while not stop.is_set():
        time.sleep(15)
        if stop.is_set():
            break
        u = _stats_user.snapshot()
        m = _stats_main.snapshot()
        ts = time.strftime("%H:%M:%S")
        print(f"\n  ── [{ts}] \u7d71\u8a08 \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
        print(f"  user_api  total={u['total']:>5}  ok={u['success']:>5}  err={u['errors']:>3}"
              f"  P50={u['p50_ms']:>6}ms  P99={u['p99_ms']:>6}ms")
        print(f"  main_app  total={m['total']:>5}  ok={m['success']:>5}  err={m['errors']:>3}"
              f"  P50={m['p50_ms']:>6}ms  P99={m['p99_ms']:>6}ms")
        print()


def _prom_instant(expr: str) -> str:
    """向 Prometheus 查詢 instant 值，多個 series 自動加總。回傳格式化字串。"""
    try:
        r = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": expr},
            timeout=5,
        )
        results = r.json().get("data", {}).get("result", [])
        if not results:
            return "N/A"
        vals = [float(res["value"][1]) for res in results
                if res["value"][1] not in ("NaN", "+Inf", "-Inf")]
        if not vals:
            return "N/A"
        total = sum(vals)
        if total == 0:
            return "0"
        if total < 0.0001:
            return f"{total:.6f}"
        if total < 1:
            return f"{total:.4f}"
        if total < 1000:
            return f"{total:.3f}"
        return f"{total:.1f}"
    except Exception:
        return "ERR"


def _show_metrics(phase: str, items: list) -> None:
    """印出 Prometheus 即時指標快照，並標示對應的 Grafana Row → Panel 位置。"""
    pad = max(len(lbl) for lbl, _, _ in items)
    bar = "─" * max(2, 45 - len(phase))
    print(f"\n  ── 📊 {phase} 指標快照 {bar}")
    for label, expr, location in items:
        val = _prom_instant(expr)
        print(f"  {label:<{pad}}  {val:>10}    📍 Grafana: {location}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# 互動式混沌示範
# ──────────────────────────────────────────────────────────────────────────────

def run_interactive(user_rps: float, main_rps: float, show_stats: bool = False):
    """
    五個 Phase 的互動式示範，讓 Grafana 全部面板都產生豐富變化：
      - scatter plot 的 Y 值上升（user_api chaos）
      - Redis Cache Hit Rate 下降 / DB 面板延遲上升（main app log storm）
      - DB Active Connections Held 峰值（connection exhaust）
    """
    stop  = threading.Event()
    n_u   = 8    # user_api 工作執行緒數
    n_m   = 4    # main app 工作執行緒數

    threads = (
        [threading.Thread(target=_user_worker, args=(user_rps, stop), daemon=True) for _ in range(n_u)]
        + [threading.Thread(target=_main_worker, args=(main_rps, stop), daemon=True) for _ in range(n_m)]
        + ([threading.Thread(target=_stats_printer, args=(stop,), daemon=True)] if show_stats else [])
    )
    for t in threads:
        t.start()

    def _wait(prompt="按 Enter 繼續..."):
        input(f"\n  >>> {prompt}")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"  ⏱  {ts}  ← 記錄此時間點，可在 Grafana 時間軸對應")

    print(SEPARATOR)
    print("  USER TRAFFIC GENERATOR \u2014 \u4e94\u968e\u6bb5\u4e92\u52d5\u6df7\u6c8c\u793a\u7bc4")
    print(SEPARATOR)
    print(f"  user_api  http://localhost:8001  Swagger: http://localhost:8001/docs")
    print(f"  main app  http://localhost:8000")
    print(f"  \u4e26\u767c\u57f7\u884c\u7dd2\uff1auser_api x{n_u} ({user_rps} RPS each) / main_app x{n_m} ({main_rps} RPS each)")
    print(f"  \u7e3d\u6d41\u91cf\uff1a~{n_u*user_rps+n_m*main_rps:.0f} RPS")
    print()

    try:
        # ─── Phase 1: 正常基線 ────────────────────────────────────────────────
        print(SEPARATOR)
        print("  PHASE 1: \u6b63\u5e38\u57fa\u7dda\uff08Normal Baseline\uff09")
        print(SEPARATOR)
        print("  \u540c\u6642\u6253 user_api + main app\uff0c\u5efa\u7acb\u5404\u9762\u677f\u7684\u57fa\u7dda\u8cc7\u6599")
        print()
        print("  Grafana \u89c0\u5bdf\uff08Row \u540d\u7a31\u5982 Grafana \u9801\u9762\u6240\u793a\uff09\uff1a")
        print("    [HTTP Traffic]       \u2192 HTTP Traffic - Request Rate (RPS)\uff1a\u5169\u500b\u670d\u52d9\u90fd\u6709\u7a69\u5b9a\u6d41\u91cf")
        print("    [HTTP Traffic]       \u2192 Response Time Scatter (User API)\uff1a\u6563\u9ede Y \u5024 < 50ms")
        print("    [Redis Cache]        \u2192 Cache Hit Rate\uff1a\u6e10\u6f38\u7a69\u5b9a\uff08cache \u91cd\u5efa\u4e2d\uff09")
        _wait("\u78ba\u8a8d Grafana \u5df2\u958b\u555f\u5f8c\u6309 Enter \u958b\u59cb\u767c\u9001\u57fa\u7dda\u6d41\u91cf...")
        print(f"  [INFO] \u57fa\u7dda\u6d41\u91cf\u9032\u884c\u4e2d\uff0c\u8acb\u7b49\u5f85 30 \u79d2\u8b93 Grafana \u8b8a\u5316...")
        time.sleep(30)
        _show_metrics("Phase 1 基線", [
            ("HTTP RPS (全服務 main+user)",
             "sum(rate(http_requests_total[1m])) + sum(rate(user_api_requests_total[1m]))",
             "HTTP Traffic → HTTP Traffic - Request Rate (RPS)"),
            ("Redis 命中率 % (全服務)",
             "(sum(rate(redis_cache_hits_total[1m]))+sum(rate(user_api_cache_hits_total[1m]))) / "
             "((sum(rate(redis_cache_hits_total[1m]))+sum(rate(user_api_cache_hits_total[1m])))"
             "+(sum(rate(redis_cache_misses_total[1m]))+sum(rate(user_api_cache_misses_total[1m])))) * 100",
             "Redis Cache → Cache Hit Rate"),
            ("DB P99 SELECT (s)",
             'histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket{query_type="select"}[1m])) by (le))',
             "資料庫層（PostgreSQL）→ DB Query Duration P99 [select]"),
            ("DB P99 INSERT (s)",
             'histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket{query_type="insert"}[1m])) by (le))',
             "資料庫層（PostgreSQL）→ DB Query Duration P99 [insert]"),
        ])

        # ─── Phase 2: user_api Log Storm → scatter Y 值跳升 ───────────────────
        print()
        print(SEPARATOR)
        print("  PHASE 2: user_api Log Storm\uff08scatter \u8b8a\u6162\uff09")
        print(SEPARATOR)
        print("  \u555f\u52d5 user_api /chaos/log-storm/start\uff1a")
        print("    \u6bcf\u7b46\u8acb\u6c42\u5728\u56de\u61c9\u8def\u5f91\u4e0a\u52a0\u5165 50~200ms \u5ef6\u9072 + 5x DB log writes")
        print("    \u9019\u6a21\u64ec\u300c\u4e0a\u7dda\u4e86\u540c\u6b65\u5beb log \u5230 DB\u300d\u7684\u53cd\u6a21\u5f0f")
        print()
        print("  Grafana \u89c0\u5bdf\uff1a")
        print("    [HTTP Traffic]           \u2192 Response Time Scatter (User API)\uff1aP99 \u5f9e < 50ms \u8df3\u5230 200ms+")
        print("    [Log Storm / Chaos]      \u2192 Log Storm Active Gauge\uff1aUser API (Host 8001) \u8b8a\u70ba 1\uff08\u7d05\u8272\uff09")
        print("    [Log Storm / Chaos]      \u2192 Log Storm - Write Rate\uff1aUser API Log Writes/s \u7dda\u689d\u653b\u5347")
        print("    [Log Storm / Chaos]      \u2192 Log Storm - Write Duration P99\uff1aUser API \u5beb\u5165\u8017\u6642\u4e0a\u5347")
        _wait("\u6309 Enter \u555f\u52d5 user_api Log Storm\u2026")
        _chaos_user_start()
        print(f"  [INFO] Log Storm \u5df2\u555f\u52d5\uff01\u89c0\u5bdf scatter plot 40 \u79d2...")
        time.sleep(40)
        _show_metrics("Phase 2 user_api Log Storm \u5cf0\u5024", [
            ("User API Log Storm \u72c0\u614b",  "user_api_log_storm_active",
             "Log Storm / Chaos Indicators \u2192 Log Storm Active"),
            ("User API log writes/s",          "sum(rate(user_api_log_writes_total[1m]))",
             "Log Storm / Chaos Indicators \u2192 Log Storm - Write Rate (writes/s)"),
            ("User API P99 latency (s)",        "histogram_quantile(0.99, sum(rate(user_api_request_duration_seconds_bucket[1m])) by (le))",
             "HTTP Traffic \u2192 Response Time Scatter (User API)"),
        ])

        # ─── Phase 3: 主 app Log Storm → Redis/DB 面板全部動起來 ────────────────
        print()
        print(SEPARATOR)
        print("  PHASE 3: \u4e3b app Log Storm\uff08Redis/DB \u9762\u677f\u5287\u8b8a\uff09")
        print(SEPARATOR)
        print("  \u555f\u52d5 main app /api/anomaly/log-storm/start\uff1a")
        print("    \u4e3b app \u6bcf\u7b46 /api/data \u540c\u6b65\u5beb DB log\uff0c\u5c0d\u5171\u4eab PostgreSQL \u9020\u6210\u58d3\u529b")
        print("    user_api + main app \u5171\u7528\u540c\u4e00\u500b DB \u3014 \u58d3\u529b\u53ef\u80fd\u6fe0\u6f2b\u81f3 user_api")
        print()
        print("  Grafana \u89c0\u5bdf\uff1a")
        print("    [Log Storm / Chaos]      \u2192 Log Storm Active Gauge\uff1aMain App (Docker 8000) \u8b8a\u70ba 1\uff08\u7d05\u8272\uff09")
        print("    [Log Storm / Chaos]      \u2192 Log Storm - Write Rate\uff1aMain App Log Writes/s \u7dda\u689d\u653b\u5347")
        print("    [Log Storm / Chaos]      \u2192 Log Storm - Write Duration P99\uff1aMain App \u5beb\u5165\u8017\u6642\u4e0a\u5347")
        print("    [Redis Cache]            \u2192 Cache Hit Rate\uff1a\u53ef\u80fd\u56e0 DB \u58d3\u529b\u800c\u6ce2\u52d5")
        print("    [\u8cc7\u6599\u5eab\u5c64\uff08PostgreSQL\uff09] \u2192 DB Query Duration P99\uff1a\u6574\u9ad4 DB \u58d3\u529b\u4e0a\u5347")
        print("    [\u8cc7\u6599\u5eab\u5c64\uff08PostgreSQL\uff09] \u2192 DB Errors\uff1a\u89f8\u767c 3 \u6b21\u5f37\u5236 DB \u932f\u8aa4\uff08operation=forced_error\uff09")
        _wait("\u6309 Enter \u555f\u52d5\u4e3b app Log Storm\u2026")
        _chaos_main_start()
        print(f"  [INFO] \u4e3b app Log Storm \u5df2\u555f\u52d5\uff01\u89c0\u5bdf Redis/DB \u9762\u677f 40 \u79d2...")
        # 在 log storm 期間分散觸發 3 次強制 DB 錯誤，讓 db_errors 面板有可見資料
        time.sleep(10)
        _chaos_trigger_db_errors(count=3, interval_s=3.0)
        time.sleep(21)  # 剩餘等待，合計 ~40 秒
        _show_metrics("Phase 3 Main App Log Storm \u5cf0\u5024", [
            ("Main App Log Storm \u72c0\u614b",  "log_storm_active",
             "Log Storm / Chaos Indicators \u2192 Log Storm Active"),
            ("Main log writes/s",              "sum(rate(log_writes_total[1m]))",
             "Log Storm / Chaos Indicators \u2192 Log Storm - Write Rate (writes/s)"),
            ("DB P99 SELECT (s)",
             'histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket{query_type="select"}[1m])) by (le))',
             "資料庫層（PostgreSQL）→ DB Query Duration P99 [select]"),
            ("DB P99 INSERT (s)",
             'histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket{query_type="insert"}[1m])) by (le))',
             "資料庫層（PostgreSQL）→ DB Query Duration P99 [insert]"),
            ("DB \u932f\u8aa4/s",               "sum(rate(db_errors_total[1m]))",
             "\u8cc7\u6599\u5eab\u5c64\uff08PostgreSQL\uff09 \u2192 DB Errors"),
        ])

        # ─── Phase 4: Connection Exhaust → DB Active Connections 出現 ───────────
        print()
        print(SEPARATOR)
        print("  PHASE 4: DB \u9023\u7dda\u6c60\u8017\u76e1\uff08db_active_connections_held \u5d4c\u5165\uff09")
        print(SEPARATOR)
        print("  \u5728 log storm \u4e0b\u53e6\u4f54\u4f4f 18/20 \u689d DB \u9023\u7dda 10 \u79d2")
        print("  \u9019\u6a21\u64ec\u300c\u7cfb\u7d71\u5728\u81e8\u754c\u9ede\uff0c\u518d\u4e00\u500b\u89f8\u767c\u9ede\u5c31\u5b8c\u5168\u7121\u56de\u61c9\u300d")
        print()
        print("  Grafana \u89c0\u5bdf\uff1a")
        print("    [Log Storm / Chaos]      \u2192 DB Active Connections Held Gauge\uff1a\u8df3\u5347\u81f3 18\uff08\u7d05\u8272\u8b66\u621d\uff09")
        print("    [HTTP Traffic]           \u2192 HTTP 5xx Error Rate\uff1a\u51fa\u73fe 503 \u932f\u8aa4\u5c16\u5cf0")
        print("    [HTTP Traffic]           \u2192 Request Latency Percentiles\uff1aP99 \u53ef\u80fd\u885d\u7834 2~3 \u79d2")
        print("    [\u8cc7\u6599\u5eab\u5c64\uff08PostgreSQL\uff09] \u2192 DB Errors\uff1a\u9023\u7dda\u6c60\u88ab\u4f54\u671f\u9593\u89f8\u767c forced_error")
        _wait("\u6309 Enter \u89f8\u767c\u9023\u7dda\u6c60\u8017\u76e1\uff08\u9078\u64c7\u6027\uff09\u2026")
        _chaos_connection_exhaust()
        print("  [INFO] \u9023\u7dda\u6c60\u5c07\u5728 10 \u79d2\u5f8c\u81ea\u52d5\u91cb\u653e\uff0c\u89c0\u5bdf Grafana...")
        # 連線池被佔用的 10 秒內，觸發強制 DB 錯誤讓面板更清晰
        time.sleep(3)
        _chaos_trigger_db_errors(count=2, interval_s=4.0)
        time.sleep(11)  # 等待連線池釋放後再繼續，合計 ~20 秒
        _show_metrics("Phase 4 \u9023\u7dda\u6c60\u8017\u76e1\u5cf0\u5024", [
            ("DB \u9023\u7dda\u4f54\u7528\u6578",              "db_active_connections_held",
             "Log Storm / Chaos Indicators \u2192 DB Active Connections Held"),
            ("HTTP 5xx errors/s (全服務)",
             "sum(rate(http_requests_total{http_status=~'5..'}[1m])) + sum(rate(user_api_requests_total{status_code=~'5..'}[1m]))",
             "HTTP Traffic → HTTP 5xx Error Rate"),
            ("DB \u932f\u8aa4/s",               "sum(rate(db_errors_total[1m]))",
             "\u8cc7\u6599\u5eab\u5c64\uff08PostgreSQL\uff09 \u2192 DB Errors"),
        ])

        # ─── Phase 5: 恢復 ────────────────────────────────────────────────────
        print()
        print(SEPARATOR)
        print("  PHASE 5: \u5168\u9762\u6062\u5fa9\uff08Recovery\uff09")
        print(SEPARATOR)
        print("  \u505c\u6b62\u6240\u6709 chaos\uff0c\u89c0\u5bdf\u5404\u9762\u677f\u5e73\u7a69\u56de\u843d")
        _wait("\u6309 Enter \u505c\u6b62\u5168\u90e8 chaos\u2026")
        _chaos_user_stop()
        _chaos_main_stop()
        print("  [INFO] \u5168\u90e8 chaos \u5df2\u505c\u6b62\uff0c\u89c0\u5bdf Grafana 30~60 \u79d2\u5167\u56de\u5230\u57fa\u7dda...")
        time.sleep(30)
        _show_metrics("Phase 5 \u6062\u5fa9\u5f8c\u57fa\u7dda", [
            ("Main App Log Storm \u72c0\u614b",  "log_storm_active",
             "Log Storm / Chaos Indicators \u2192 Log Storm Active\uff08\u61c9\u6b78\u96f6\uff09"),
            ("User API Log Storm \u72c0\u614b",  "user_api_log_storm_active",
             "Log Storm / Chaos Indicators \u2192 Log Storm Active\uff08\u61c9\u6b78\u96f6\uff09"),
            ("DB \u9023\u7dda\u4f54\u7528\u6578",              "db_active_connections_held",
             "Log Storm / Chaos Indicators \u2192 DB Active Connections Held\uff08\u61c9\u6b78\u96f6\uff09"),
            ("HTTP RPS (全服務 main+user)",
             "sum(rate(http_requests_total[1m])) + sum(rate(user_api_requests_total[1m]))",
             "HTTP Traffic → HTTP Traffic - Request Rate (RPS)（應回到基線）"),
        ])

        # ─── Phase 6: HA Failover（Patroni 主從切換）──────────────────────────
        print()
        print(SEPARATOR)
        print("  PHASE 6: HA Failover 示範（Patroni + etcd 主從切換）")
        print(SEPARATOR)
        print("  模擬 Primary 節點機器猝死（docker stop），觀察自動 Failover：")
        print("    1. Patroni 停止更新 etcd Leader Lease（Heartbeat 中斷）")
        print("    2. etcd TTL(30s) 到期 → 其餘 Patroni 競爭新 Leader Lease")
        print("    3. WAL 最新的 Replica 勝出 → 升格為新 Primary")
        print("    4. HAProxy 在下一個 check 週期（≈3~9s）自動重新路由")
        print("    5. 應用程式自動重連到新 Primary，服務恢復")
        print()
        print("  Grafana 觀察：")
        print("    [HTTP Traffic]           → HTTP 5xx Error Rate：Failover 期間短暫上升（10~30 秒）")
        print("    [HTTP Traffic]           → Request Latency Percentiles：P99 衝高後恢復")
        print("    [Log Storm / Chaos]      → DB Active Connections Held：切換瞬間歸零再恢復")
        print()
        print("  HAProxy Stats：http://localhost:7001/stats")
        print("  Patroni 叢集狀態：curl http://localhost:8081/cluster | python3 -m json.tool")
        _wait("按 Enter 觸發 Failover（停止當前 Primary 容器）…")

        stopped_container = _chaos_failover_primary()
        if stopped_container:
            print(f"\n  [INFO] 等待 Patroni Failover（etcd TTL=30s + HAProxy check ≈3s）...")
            print(f"  [INFO] 監控新 Primary 選出...")

            # 監控 Failover 進度（每 5 秒輪詢一次 Patroni REST API）
            new_primary_found = False
            for attempt in range(12):  # 最多等 60 秒
                time.sleep(5)
                new = _chaos_get_primary()
                if new and new["name"] != stopped_container.split("-")[1]:
                    print(f"\n  [RECOVERY] ✓ 新 Primary：{new['name']}（{5*(attempt+1)}秒後選出）")
                    new_primary_found = True
                    break
                elif new:
                    print(f"  [INFO] 仍在等待 Failover... ({5*(attempt+1)}s 已過)")
                else:
                    print(f"  [INFO] Patroni 正在選舉中... ({5*(attempt+1)}s 已過)")

            if not new_primary_found:
                print("  [WARN] 超過 60 秒仍未偵測到新 Primary，請手動確認叢集狀態")

            print()
            print(f"  [INFO] 觀察 Grafana 錯誤率回落（建議等待 30 秒）...")
            time.sleep(30)

            # 重啟舊 Primary，讓它以 Replica 身份回歸
            _wait(f"按 Enter 重啟舊 Primary 容器（{stopped_container}，將以 Replica 加入）…")
            _chaos_restart_container(stopped_container)
            print("  [INFO] 等待舊節點以 Replica 身份加入（約 30 秒）...")
            time.sleep(30)
            cluster_state = _chaos_get_primary()
            if cluster_state:
                print(f"  [INFO] 叢集恢復，當前 Primary：{cluster_state['name']}")
            time.sleep(10)  # 等指標穩定
            _show_metrics("Phase 6 HA Failover 恢復後", [
                ("HTTP RPS (全服務 main+user)",
                 "sum(rate(http_requests_total[1m])) + sum(rate(user_api_requests_total[1m]))",
                 "HTTP Traffic → HTTP Traffic - Request Rate (RPS)（Failover 後應回升）"),
                ("HTTP 5xx errors/s (全服務)",
                 "sum(rate(http_requests_total{http_status=~'5..'}[1m])) + sum(rate(user_api_requests_total{status_code=~'5..'}[1m]))",
                 "HTTP Traffic → HTTP 5xx Error Rate（Failover 結束後應歸零）"),
                ("DB P99 SELECT (s)",
                 'histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket{query_type="select"}[1m])) by (le))',
                 "資料庫層（PostgreSQL）→ DB Query Duration P99（新 Primary 接手後延遲）"),
            ])
        else:
            print("  [SKIP] 跳過 Failover 示範（無法偵測 Patroni 節點）")
            print("         若未使用 HA 架構，此 Phase 會自動跳過")
            time.sleep(5)

        # 結束
        stop.set()
        u = _stats_user.snapshot()
        m = _stats_main.snapshot()
        print()
        print(SEPARATOR)
        print("  示範完成")
        print(SEPARATOR)
        print(f"  user_api:  total={u['total']}  P50={u['p50_ms']}ms  P99={u['p99_ms']}ms")
        print(f"  main_app:  total={m['total']}  P50={m['p50_ms']}ms  P99={m['p99_ms']}ms")
        print()
        print("  建議保存 Grafana 截圖，比較 Phase 1↔Phase 3↔Phase 6 的變化")
        print("  HAProxy Stats：http://localhost:7001/stats")

    except KeyboardInterrupt:
        print("\n\n  [INTERRUPT] \u4f7f\u7528\u8005\u4e2d\u65b7\uff0c\u57f7\u884c\u5b89\u5168\u6062\u5fa9...")
    finally:
        stop.set()
        _chaos_user_stop()
        _chaos_main_stop()
        print("  \u5df2\u505c\u6b62\u3002")


# ──────────────────────────────────────────────────────────────────────────────
# 持續執行模式（非互動）
# ──────────────────────────────────────────────────────────────────────────────

def run_continuous(user_rps: float, main_rps: float, chaos: str, chaos_duration: int | None, show_stats: bool = False):
    stop  = threading.Event()
    n_u   = 8
    n_m   = 4

    threads = (
        [threading.Thread(target=_user_worker, args=(user_rps, stop), daemon=True) for _ in range(n_u)]
        + [threading.Thread(target=_main_worker, args=(main_rps, stop), daemon=True) for _ in range(n_m)]
        + ([threading.Thread(target=_stats_printer, args=(stop,), daemon=True)] if show_stats else [])
    )
    for t in threads:
        t.start()

    print(SEPARATOR)
    print("  USER TRAFFIC GENERATOR \u2014 \u6301\u7e8c\u6a21\u5f0f")
    print(SEPARATOR)
    print(f"  user_api  http://localhost:8001  (~{n_u*user_rps:.0f} RPS)")
    print(f"  main_app  http://localhost:8000  (~{n_m*main_rps:.0f} RPS)")
    print(f"  chaos     {chaos}")
    print(f"  Swagger   http://localhost:8001/docs")
    print("  Ctrl+C \u505c\u6b62\n")

    try:
        if chaos == "log-storm-user":
            _chaos_user_start()
        elif chaos == "log-storm-all":
            _chaos_user_start()
            _chaos_main_start()

        if chaos_duration and chaos != "normal":
            print(f"  [INFO] {chaos} \u5c07\u5728 {chaos_duration} \u79d2\u5f8c\u81ea\u52d5\u6062\u5fa9...")
            time.sleep(chaos_duration)
            _chaos_user_stop()
            _chaos_main_stop()
            print("  [RECOVERY] chaos \u5df2\u505c\u6b62\uff0c\u7e7c\u7e8c\u6301\u7e8c\u6d41\u91cf...")

        while True:
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n  [STOP] \u6536\u5230\u4e2d\u65b7\u8a0a\u865f...")
    finally:
        stop.set()
        _chaos_user_stop()
        _chaos_main_stop()
        u = _stats_user.snapshot()
        m = _stats_main.snapshot()
        print(f"\n  \u6700\u7d42\u7d71\u8a08\uff1auser_api P99={u['p99_ms']}ms  main_app P99={m['p99_ms']}ms")
        print("  \u5df2\u505c\u6b62\u3002")


# ──────────────────────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────────────────────

def _check_services() -> bool:
    ok = True
    for url, name in [(USER_API_URL, "user_api"), (MAIN_API_URL, "main app")]:
        try:
            r = requests.get(f"{url}/health", timeout=3)
            icon = "✓" if r.status_code == 200 else "?"
            print(f"  {icon} {name}: {url}")
        except Exception:
            print(f"  ✗ {name}: {url}  ← 無法連線")
            ok = False
    return ok


def main():
    parser = argparse.ArgumentParser(
        description="user_api.py + main app 雙服務流量產生器（含混沌功能）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
範例：
  python user_traffic_generator.py                                # 持續正常流量
  python user_traffic_generator.py --interactive                  # 互動式簡報示範（不顯示統計）
  python user_traffic_generator.py --interactive --stats          # 互動式 + 每 15 秒顯示統計
  python user_traffic_generator.py --stats                        # 持續流量 + 顯示統計
  python user_traffic_generator.py --chaos log-storm-user         # user_api 慢化
  python user_traffic_generator.py --chaos log-storm-all --chaos-duration 60
        """,
    )
    parser.add_argument("--user-rps",  type=float, default=12.0,
                        help="user_api 每執行緒 RPS（預設 12，共 8 執行緒 = ~96 RPS）")
    parser.add_argument("--main-rps",  type=float, default=15.0,
                        help="main app 每執行緒 RPS（預設 15，共 4 執行緒 = ~60 RPS）")
    parser.add_argument("--chaos", choices=["normal", "log-storm-user", "log-storm-all"],
                        default="normal")
    parser.add_argument("--chaos-duration", type=int, default=None,
                        help="chaos 持續秒數後自動恢復（無此參數則持續）")
    parser.add_argument("--interactive", action="store_true",
                        help="互動式五階段示範（含暫停點，適合 Grafana 對照展示）")
    parser.add_argument("--stats", action="store_true",
                        help="每 15 秒印出 RPS / P50 / P99 統計（預設關閉）")
    args = parser.parse_args()

    # ── 建立 log 檔，Tee 同步寫入 console + 檔案 ──
    log_path = f"traffic_test_{time.strftime('%Y%m%d_%H%M%S')}.log"
    _log_fh   = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(sys.__stdout__, _log_fh)
    print(f"  [LOG] 本次執行記錄同步寫入：{log_path}")
    print()

    try:
        print("\n  確認服務中...")
        if not _check_services():
            print("\n  ✗ 請確認兩個服務都已啟動：")
            print("    python user_api.py")
            print("    docker compose up -d")
            sys.exit(1)
        print()

        if args.interactive:
            run_interactive(args.user_rps, args.main_rps, args.stats)
        else:
            run_continuous(args.user_rps, args.main_rps, args.chaos, args.chaos_duration, args.stats)
    finally:
        sys.stdout = sys.__stdout__
        _log_fh.close()
        print(f"\n  [LOG] 已完整儲存至：{log_path}")


if __name__ == "__main__":
    main()

