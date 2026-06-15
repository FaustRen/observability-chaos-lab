<div align="right">

[English](README.md) · **繁體中文**

</div>

# API Monitoring · 後端可觀測性與混沌工程實驗平台

一套可在本機完整運行的後端系統，示範 **高可用架構（HA）**、**RED 方法可觀測性（Observability）** 與 **混沌工程（Chaos Engineering）** 的端到端實作。整體環境由 **18 個 Docker 容器 + 1 個主機服務** 組成，可一鍵啟動、注入故障、觀察指標、驗證自動 Failover。

---

## ✨ 核心特色

| 面向 | 內容 |
|---|---|
| **雙重高可用** | PostgreSQL（Patroni + etcd Raft + HAProxy）與 Redis（Sentinel）皆具備自動 Failover |
| **混合儲存** | Cache-Aside 模式：Redis 快取 + PostgreSQL 持久層 |
| **完整可觀測性** | Prometheus 採集 + Grafana 視覺化，涵蓋 HTTP → Cache → DB → System 四層觀測鏈 |
| **混沌工程** | 多個故障注入端點（延遲、錯誤、連線池枯竭、快取清空、節點下線等） |
| **分層 OOP 設計** | Router / Repository / Metrics / Config 職責分離，App Factory 入口 |

---

## 🏗️ 系統架構

```
                              ┌──────────────┐
            觀測層            │  Grafana     │  :3000
                              │  Prometheus  │  :9090
                              └──────┬───────┘
                                     │ scrape /metrics
                              ┌──────┴───────┐
            應用層            │  FastAPI     │  :8000   ← Cache-Aside 業務邏輯
                              └──┬────────┬──┘
                  Sentinel HA    │        │   Patroni HA
              ┌──────────────────┘        └──────────────────┐
        ┌─────┴──────┐                              ┌─────────┴────────┐
        │ Redis      │ Master + 2 Replica           │ HAProxy          │ :5432 寫 / :5433 讀
        │ + 3 Sentinel                              │ Patroni ×3       │
        └────────────┘                              │ etcd ×3 (Raft)   │
                                                    └──────────────────┘
```

詳細圖文說明請見 [document/project_wiki.pdf](document/project_wiki.pdf) 與 [document/system_architecture.pdf](document/system_architecture.pdf)。

---

## 🧱 技術棧

- **應用框架**：FastAPI (Python 3.x)
- **快取高可用**：Redis Master/Replica + Redis Sentinel（quorum=2）
- **資料庫高可用**：PostgreSQL + Patroni + etcd（Raft 共識）+ HAProxy
- **可觀測性**：Prometheus + Grafana + redis_exporter + postgres_exporter
- **容器編排**：Docker Compose（含 healthcheck 依賴鏈）

---

## 🚀 快速開始

### 事前需求
- Docker Desktop（已啟動）
- Python 3.x，並安裝 `requests`：`pip install requests`

### 啟動
```bash
docker compose up -d        # 啟動全部 18 個容器
docker compose ps           # 確認皆為 Up / healthy
```

### 驗證
| 服務 | 位址 |
|---|---|
| API 健康檢查 | http://localhost:8000/ |
| Prometheus 指標 | http://localhost:8000/metrics/ |
| Prometheus UI | http://localhost:9090 |
| Grafana | http://localhost:3000 （admin / 密碼見 `.env`） |

> 主機端示範服務（選用）：`python3 user_api.py` → http://localhost:8001/docs

完整啟動、匯入 Dashboard 與測試流程請見 [document/usage.txt](document/usage.txt)。

---

## 🔌 API 端點

### 業務端點
| 方法 | 路徑 | 說明 |
|---|---|---|
| GET | `/data` | Cache-Aside 讀取（先查 Redis，未命中再查 PostgreSQL 並回填） |
| GET | `/write` | 寫入資料並使快取失效 |

### 混沌注入端點（`/anomaly/*`）
| 路徑 | 模擬情境 |
|---|---|
| `/lag` | 注入回應延遲 |
| `/error` | 注入應用層錯誤 |
| `/db-overload` | 資料庫高負載 |
| `/db-error` | 資料庫錯誤 |
| `/cache-flush` | 清空 Redis 快取 |
| `/redis-down` | 模擬 Redis 主節點下線（觸發 Sentinel Failover） |
| `/connection-exhaust` | 連線池枯竭反模式 |
| `/log-storm/{start,stop,status}` | 日誌風暴開關與狀態 |

---

## 📊 監控與儀表板

`grafana_dashboard.json` 為 Grafana v2 API 格式，需以指令匯入（非 UI Import）。
匯入步驟詳見 [document/usage.txt](document/usage.txt)。儀表板涵蓋：

- **RED Method**：Rate / Errors / Duration（含 P99）
- **Cache 命中率與延遲**
- **資料庫連線、查詢延遲與 Failover 狀態**
- **系統資源（CPU / Memory）**

---

## 📸 實機展示 — 儀表板結果

以下截圖是在執行完整混沌測試手冊時，從 Grafana 即時擷取而來。每張圖對應 [document/sre_testing_runbook.md](document/sre_testing_runbook.md) 中的一個階段。

### 1. 基準線（穩態）
正常流量、健康的快取命中率、零錯誤——作為後續所有故障注入的參考基準。

![基準線儀表板](document/screenshots/01_baseline.png)

### 2. user_api 日誌風暴
在主機端 `user_api` 服務觸發日誌風暴，推升其寫入速率與延遲，而系統其餘部分維持穩定。

![user_api 日誌風暴](document/screenshots/02_user_storm.png)

### 3. 全系統日誌風暴 + 資料庫錯誤
風暴擴散至整個架構並注入資料庫錯誤，點亮混沌指標與 5xx 錯誤面板。

![全系統日誌風暴與資料庫錯誤](document/screenshots/03_system_storm.png)

### 4. 連線池耗盡
刻意耗盡資料庫連線池（佔用 18/20 連線），推升查詢延遲 P99 並呈現連線飽和狀態。

![連線池耗盡](document/screenshots/04_conn_exhaust.png)

### 5. 恢復
停止故障注入後，延遲、錯誤與連線使用率回到基準線，系統自我修復。

![恢復](document/screenshots/05_recovery.png)

### 6. PostgreSQL 高可用 Failover
強制關閉 PostgreSQL 主節點。Patroni 重新選舉新 Leader 期間，可見短暫的 5xx/503 尖峰與延遲突增（RTO ≈ 5 秒）。

![高可用 Failover](document/screenshots/06_ha_failover.png)

### 7. 高可用恢復
舊主節點以串流 Replica 身分重新加入，由新選出的 Leader 提供服務——錯誤歸零，叢集再次健康。

![高可用恢復](document/screenshots/07_ha_recovered.png)

---

## 🧪 混沌與壓測腳本

| 腳本 | 用途 |
|---|---|
| `chaos_scenario.py` | 編排混沌情境，依序觸發故障注入端點 |
| `traffic_generator.py` | 對 API 產生背景流量 |
| `user_api.py` / `user_traffic_generator.py` | 主機端示範服務與其流量產生器 |

混沌與 Failover 的完整測試手冊請見 [document/sre_testing_runbook.md](document/sre_testing_runbook.md)。

---

## 📁 專案結構

```
api_monitoring/
├── app/                     # FastAPI 應用（分層 OOP）
│   ├── main.py              # App Factory 入口（lifespan / middleware / router 組裝）
│   ├── config.py            # 環境變數設定（immutable Settings）
│   ├── metrics.py           # Prometheus 指標定義
│   ├── repositories/        # 資料存取層（cache_repo / item_repo）
│   └── routers/             # 路由層（items / anomaly）
├── haproxy/                 # HAProxy 設定（讀寫分流）
├── patroni/                 # Patroni 節點設定
├── prometheus/              # Prometheus 採集設定
├── docker-compose.yml       # 18 容器編排
├── grafana_dashboard.json   # Grafana 儀表板定義
├── chaos_scenario.py        # 混沌情境腳本
├── traffic_generator.py     # 流量產生器
└── document/                # 架構文件、簡報與測試手冊（PDF）
```

---

## 📚 延伸文件

| 文件 | 繁體中文 | English |
|---|---|---|
| 專案 Wiki | [PDF](document/project_wiki.pdf) | [PDF](document/project_wiki.en.pdf) |
| 簡報 | [PDF](document/presentation_slides.pdf) | [PDF](document/presentation_slides.en.pdf) |
| SRE 測試手冊 | [Markdown](document/sre_testing_runbook.md) | [Markdown](document/sre_testing_runbook.en.md) |
| 啟動與使用說明 | [Text](document/usage.txt) | [Text](document/usage.en.txt) |

圖表（不分語言）：
- [系統架構圖（PDF）](document/system_architecture.pdf)
- [類別圖（PDF）](document/class_diagram.pdf)
- [混沌情境序列圖（PDF）](document/Sequence_Chaos.pdf)

---

## 📄 授權條款

本專案以 [MIT License](LICENSE) 釋出。
