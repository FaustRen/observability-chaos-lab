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

- [專案 Wiki（PDF）](document/project_wiki.pdf)
- [簡報（PDF）](document/presentation_slides.pdf)
- [系統架構圖（PDF）](document/system_architecture.pdf)
- [類別圖（PDF）](document/class_diagram.pdf)
- [混沌情境序列圖（PDF）](document/Sequence_Chaos.pdf)
- [SRE 測試手冊](document/sre_testing_runbook.md)
- [啟動與使用說明](document/usage.txt)
