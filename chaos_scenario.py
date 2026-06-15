#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chaos_scenario.py — 分段混沌劇本腳本

模擬真實事件：「上線了同步記錄 Log 至 DB 的功能，導致系統在高流量下逐漸降級」

執行方式：
  # 終端機 1：持續產生背景流量
  python traffic_generator.py

  # 終端機 2：執行混沌劇本（有暫停點，可控制節奏）
  python chaos_scenario.py

劇本分為 5 個 Phase，每個 Phase 都會先說明狀況再執行，
給你足夠時間切換到 Grafana 觀察對應的指標變化。

安全機制：
  - 每個破壞性操作都有對應的恢復步驟
  - 腳本結束時無論中途是否中斷，都會嘗試停止 log storm
  - Ctrl+C 可隨時中止，恢復步驟仍會被執行
"""

import sys
import time
import threading
import requests

BASE_URL = "http://localhost:8000"
SEPARATOR = "=" * 65


def _print_phase(num: int, title: str, description: str) -> None:
    print(f"\n{SEPARATOR}")
    print(f"  PHASE {num}: {title}")
    print(SEPARATOR)
    print(description)
    print()


def _wait_for_user(prompt: str = "按 Enter 繼續...") -> None:
    input(f"  >>> {prompt}")


def _call(method: str, path: str, label: str = "") -> dict | None:
    url = f"{BASE_URL}{path}"
    try:
        resp = requests.request(method, url, timeout=15)
        tag = f"[{label}] " if label else ""
        print(f"  {tag}HTTP {resp.status_code}  {path}")
        return resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except requests.exceptions.RequestException as exc:
        print(f"  [ERROR] {path} → {exc}")
        return None


def _burst_traffic(duration_seconds: int, rps: int = 10) -> None:
    """在背景持續發送高強度流量 duration_seconds 秒。"""
    stop_event = threading.Event()
    interval = 1.0 / rps
    sent = {"count": 0}

    def _worker():
        endpoints = ["/api/data"] * 7 + ["/api/write"] * 2 + ["/api/anomaly/lag"]
        idx = 0
        while not stop_event.is_set():
            try:
                requests.get(f"{BASE_URL}{endpoints[idx % len(endpoints)]}", timeout=5)
                sent["count"] += 1
            except Exception:
                pass
            idx += 1
            time.sleep(interval)

    # 4 個並發執行緒模擬多用戶同時請求
    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(4)]
    for t in threads:
        t.start()

    print(f"  [TRAFFIC] 開始發送 ~{rps * 4} RPS 高流量，持續 {duration_seconds} 秒...")
    for remaining in range(duration_seconds, 0, -5):
        time.sleep(5)
        print(f"  [TRAFFIC] 剩餘 {remaining - 5} 秒，已發送 {sent['count']} 個請求")

    stop_event.set()
    for t in threads:
        t.join(timeout=2)
    print(f"  [TRAFFIC] 高流量結束，共發送 {sent['count']} 個請求")


def stop_log_storm_safe() -> None:
    """安全停止 log storm，即使其他步驟失敗也會被呼叫。"""
    try:
        resp = requests.get(f"{BASE_URL}/api/anomaly/log-storm/stop", timeout=5)
        if resp.status_code == 200:
            print("\n  [RECOVERY] Log storm 已停止，系統恢復正常。")
        else:
            print(f"\n  [RECOVERY] Log storm stop 回傳 {resp.status_code}")
    except Exception as exc:
        print(f"\n  [RECOVERY] 無法停止 log storm（API 可能已關閉）: {exc}")


def main() -> None:
    print(SEPARATOR)
    print("  CHAOS SCENARIO: Synchronous DB Logging Cascade Failure")
    print("  重現真實事件：上線 DB 寫 Log 功能導致系統降級")
    print(SEPARATOR)
    print()
    print("  前置確認：")
    print("  1. docker compose 已啟動（docker ps 確認 7 個容器都是 Up）")
    print("  2. traffic_generator.py 正在另一個終端機執行中")
    print("  3. Grafana 已開啟（localhost:3000），切到你的 Dashboard")
    print()

    _wait_for_user("確認以上都準備好後按 Enter 開始劇本")

    # ------------------------------------------------------------------ #
    # Phase 1：確認系統健康基線
    # ------------------------------------------------------------------ #
    _print_phase(
        1,
        "確認系統基線（Baseline）",
        "  確認系統目前運作正常。\n"
        "  在 Grafana 觀察：\n"
        "    - Request Rate：應有穩定流量（來自 traffic_generator）\n"
        "    - Latency P99：應低於 100ms（正常路徑）\n"
        "    - Cache Hit Rate：應在 50~90%\n"
        "    - log_storm_active：應為 0",
    )

    result = _call("GET", "/", "Health Check")
    result = _call("GET", "/api/anomaly/log-storm/status", "Log Storm Status")
    if result:
        print(f"  log_storm_active = {result.get('log_storm_active', '?')}")

    print()
    _wait_for_user("觀察 Grafana 基線後按 Enter 進入 Phase 2")

    # ------------------------------------------------------------------ #
    # Phase 2：模擬「上線新功能」— 啟動同步 DB 寫 Log
    # ------------------------------------------------------------------ #
    _print_phase(
        2,
        "模擬部署：啟動同步 DB 寫 Log（log storm）",
        "  這模擬一個開發人員上線了「記錄 API request log 至 DB」的功能。\n"
        "  在開發/測試環境因流量低所以沒有問題，但 production 流量下...\n\n"
        "  效果：之後每個 /api/data 請求都會同步執行一次 DB INSERT（50~150ms）\n"
        "  在 Grafana 即將觀察到：\n"
        "    - log_storm_active：變為 1（立即）\n"
        "    - log_writes_total：開始快速累積\n"
        "    - http_request_duration_seconds P99：逐漸爬升（30~60 秒後明顯）",
    )

    _wait_for_user("按 Enter 啟動 log storm（模擬新功能上線）")
    _call("GET", "/api/anomaly/log-storm/start", "DEPLOY: Log Storm START")

    print()
    print("  [INFO] Log storm 已啟動。接下來讓它在正常流量下跑 30 秒...")
    print("  [INFO] 觀察 Grafana 的 latency 是否開始緩慢上升")
    time.sleep(30)

    # ------------------------------------------------------------------ #
    # Phase 3：高流量壓力測試（系統開始降級）
    # ------------------------------------------------------------------ #
    _print_phase(
        3,
        "高流量衝擊：系統開始降級",
        "  現在在 log storm 啟動的情況下加入高強度流量。\n"
        "  模擬「上線後遇到業務高峰」的場景。\n\n"
        "  每個請求 = 1 次 DB select + 1 次 DB log INSERT（佔用連線池）\n"
        "  pool_size=5，高並發下連線池耗盡，後續請求被迫等待。\n\n"
        "  在 Grafana 觀察（這是關鍵時刻）：\n"
        "    - http_request_duration_seconds P99：明顯飆升（200ms → 1s+）\n"
        "    - log_write_duration_seconds P99：可觀察 DB 寫入延遲\n"
        "    - db_query_duration_seconds：整體 DB 壓力升高\n"
        "    - system_memory_usage_percent：因請求堆積可能緩慢上升",
    )

    _wait_for_user("按 Enter 開始高流量衝擊（持續 60 秒）")
    _burst_traffic(duration_seconds=60, rps=8)

    # ------------------------------------------------------------------ #
    # Phase 4：疊加連線池耗盡（最嚴峻狀態）
    # ------------------------------------------------------------------ #
    _print_phase(
        4,
        "疊加衝擊：連線池耗盡（選擇性執行）",
        "  在 log storm + 高流量的基礎上，額外佔住 4/5 條 DB 連線 10 秒。\n"
        "  這模擬「系統已在臨界點，再一個觸發點就完全無回應」。\n\n"
        "  在 Grafana 觀察：\n"
        "    - db_active_connections_held：升至 4\n"
        "    - http_request_duration_seconds P99：可能衝破 2~3 秒\n"
        "    - 10 秒後連線自動釋放，latency 應部分恢復",
    )

    _wait_for_user("按 Enter 觸發連線池耗盡（10 秒後自動恢復），或 Ctrl+C 跳過")
    _call("GET", "/api/anomaly/connection-exhaust", "Connection Exhaust")
    print("  [INFO] 等待 15 秒觀察 Grafana 上的衝擊效果...")
    time.sleep(15)

    # ------------------------------------------------------------------ #
    # Phase 5：恢復（Recovery）
    # ------------------------------------------------------------------ #
    _print_phase(
        5,
        "恢復（Recovery）— 停止 log storm",
        "  現在模擬 SRE 收到告警後的第一個動作：\n"
        "  「先關掉有問題的功能（log storm），讓系統恢復，再慢慢查根因」\n\n"
        "  這就是 rollback / feature flag 的本質：\n"
        "  不需要重啟容器，不需要重新部署，一個 API call 立即生效。\n\n"
        "  在 Grafana 觀察：\n"
        "    - log_storm_active：回到 0\n"
        "    - http_request_duration_seconds P99：30 秒內應恢復正常\n"
        "    - log_writes_total：停止增長（rate 趨近 0）",
    )

    _wait_for_user("按 Enter 停止 log storm（模擬 SRE 緊急回滾功能）")
    stop_log_storm_safe()

    print()
    print("  [INFO] 觀察 Grafana 30~60 秒，確認 latency 回到基線水位...")
    time.sleep(30)

    # ------------------------------------------------------------------ #
    # 結束報告
    # ------------------------------------------------------------------ #
    print(f"\n{SEPARATOR}")
    print("  CHAOS SCENARIO 完成")
    print(SEPARATOR)
    print()
    print("  這個劇本示範了：")
    print("  1. 功能上線前測試環境無法 100% 模擬真實流量壓力")
    print("  2. 同步 DB 寫 Log 在高流量下如何引發連線池耗盡與 latency 崩潰")
    print("  3. 指標（Metrics）讓問題可見：log_storm_active / log_write_duration")
    print("  4. 恢復不一定需要重啟服務：feature flag 即時切換是關鍵設計")
    print("  5. 恢復後的 Grafana 時間線就是你的「事後分析（postmortem）」證據")
    print()
    print("  Grafana 截圖建議保存，作為 Root Cause Analysis 的時間線證據。")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  [INTERRUPT] 腳本中斷，執行安全恢復...")
        stop_log_storm_safe()
        print("  腳本已結束。\n")
        sys.exit(0)
