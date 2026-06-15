<div align="right">

[English](sre_testing_runbook.en.md) · **繁體中文**

</div>

# SRE Testing Runbook — API Monitoring System

> **文件定位**：本文件為 Site Reliability Engineer（SRE）執行壓測與混沌工程的操作手冊（Runbook）。  
> **目標讀者**：SRE、後端工程師、Platform Engineer  
> **設計理念**：遵循 Google SRE 的 RED Method（Rate / Errors / Duration）和 USE Method（Utilization / Saturation / Errors）進行觀察

---

## 一、系統架構概覽

```
                     ┌─────────────────────────────────────────────────┐
                     │              Monitoring Stack (Docker)          │
                     │                                                 │
  ┌──────────────┐   │  ┌───────────────┐     ┌─────────────────────┐ │
  │ user_api.py  │──►│  │  Prometheus   │────►│      Grafana        │ │
  │ (port 8001)  │   │  │  (port 9090)  │     │    (port 3000)      │ │
  │  FastAPI     │   │  └───────────────┘     └─────────────────────┘ │
  └──────┬───────┘   │        ▲                                       │
         │           │        │ scrape                                │
         │  SQL/Redis│  ┌─────┴─────┐  ┌──────────┐  ┌───────────┐  │
         └──────────►│  │ main app  │  │  Redis   │  │ PostgreSQL│  │
                     │  │ (port8000)│  │ Exporter │  │ Exporter  │  │
                     │  └───────────┘  └──────────┘  └───────────┘  │
                     └─────────────────────────────────────────────────┘

 user_traffic_generator.py ──────────────────────────────────────────►
  (本機執行)               同時打 port 8001 + port 8000，觸發所有面板資料
```

### 服務端口速查

| 服務 | 端口 | 用途 |
|------|------|------|
| main app (FastAPI) | `8000` | 主要 API 服務（Docker） |
| user_api (FastAPI) | `8001` | 使用者模擬服務（本機執行） |
| HAProxy (write) | `5432` | 路由寫入至 Patroni Primary |
| HAProxy (read) | `5433` | 路由讀取至 Patroni Replicas |
| HAProxy Stats | `7001` | HAProxy 管理頁面 |
| Patroni REST API | `8081/8082/8083` | 叢集狀態 / 主從判斷 |
| Prometheus | `9090` | 指標採集與查詢 |
| Grafana | `3000` | 視覺化儀表板 |
| Redis | `6379` | 快取層 |

---

## 二、Pre-test Checklist（測試前確認清單）

在開始任何測試前，請依序確認以下項目：

```bash
# ① 確認 Docker 服務全部正常運行（13 個容器）
docker compose ps

# 預期輸出（所有服務 Status 為 Up）：
# api_monitoring-etcd1-1            Up (healthy)
# api_monitoring-etcd2-1            Up (healthy)
# api_monitoring-etcd3-1            Up (healthy)
# api_monitoring-patroni1-1         Up (healthy)
# api_monitoring-patroni2-1         Up (healthy)
# api_monitoring-patroni3-1         Up (healthy)
# api_monitoring-haproxy-1          Up (healthy)
# api_monitoring-api-1              Up
# api_monitoring-redis-1            Up (healthy)
# api_monitoring-prometheus-1       Up
# api_monitoring-grafana-1          Up
# api_monitoring-redis_exporter-1   Up
# api_monitoring-postgres_exporter-1 Up

# ② 確認 Patroni 叢集狀態（應有一個 leader）
curl -s http://localhost:8081/cluster | python3 -m json.tool | grep role
# 預期：一個 "leader"，兩個 "replica"

# ③ 確認 HAProxy DB 路由正常
curl -s http://localhost:7001/stats | grep -E 'pg_(write|read)'

# ④ 確認 user_api.py 正在運行
curl http://localhost:8001/health
# 預期：{"status":"ok","service":"user_api","port":8001}

# ⑤ 確認主 app 正在運行
curl http://localhost:8000/

# ⑥ 確認 Prometheus 已在 scrape 兩個 target
# 瀏覽器開啟：http://localhost:9090/targets
# 確認 "user-api" 和 "fastapi-app" 均為 UP 狀態

# ⑦ 確認 chaos 狀態乾淨（均為 0 / false）
curl http://localhost:8001/chaos/status
curl http://localhost:8000/api/anomaly/log-storm/status
```

### 重置計數器（每次測試前建議執行）

服務重啟會讓 Prometheus Counter 從 0 重新計算，讓 Grafana 面板更容易觀察變化。

```bash
# 重啟主 app（重置 http_requests_total 等計數器）
docker compose restart api

# 重啟 user_api.py（重置 user_api_requests_total 等計數器）
pkill -f "user_api.py"
python3 user_api.py &

# 在 Grafana 上，將時間範圍設定為 "Last 15 minutes"
# 這樣可以只看到測試期間的資料
```

---

## 三、啟動流程（System Start-up）

### Step 1：啟動 Docker 服務

```bash
cd /path/to/api_monitoring
docker compose up -d
```

### Step 2：啟動 user_api.py

```bash
# 方法 A：前台執行（可直接看 log，方便 debug）
python3 user_api.py

# 方法 B：背景執行（測試時不佔終端機）
python3 user_api.py > /tmp/user_api.log 2>&1 &

# 確認啟動成功
curl http://localhost:8001/health
```

### Step 3：開啟 Grafana

1. 瀏覽器開啟 `http://localhost:3000`
2. 登入：admin / `<your_password>`
3. 進入儀表板，設定時間範圍為 **Last 15 minutes**，重新整理間隔建議 **5s**

---

## 四、測試方式一：互動式五階段混沌示範

這是最完整的測試方式，適合**簡報展示**或**完整觀察所有指標變化**的場合。

```bash
python3 user_traffic_generator.py --interactive
```

每個 Phase 都有暫停點，讓你可以先在 Grafana 上確認當前狀態再繼續。

---

### Phase 1：正常基線（Normal Baseline）

**目的**：建立所有指標的健康基線值

**流量行為**：
- user_api.py：60% 搜尋（`/customer/search`）/ 30% 複合查詢（`/customer/query`）/ 10% 新增（`/customer/register`）
- main app：70% `GET /api/data` / 30% `GET /api/write`
- 總計約 **20 RPS**

**等待時間**：30 秒（讓 Redis cache warm-up，各 Panel 穩定）

#### 應觀察的指標與預期值

| Panel | 預期值 | 代表意義 |
|-------|--------|----------|
| HTTP Traffic - RPS | 穩定，約 8–15 RPS | 系統正常接收流量 |
| Request Latency P50 | < 20ms | 多數請求快速完成 |
| Request Latency P99 | < 100ms | 長尾延遲在可接受範圍 |
| Cache Hit Rate | 逐漸爬升至 > 60% | Redis Cache Warm-up 效應 |
| HTTP 5xx Error Rate | 0 | 無任何錯誤 |
| Log Storm Active | 0 | Chaos 未啟動 |
| DB Active Connections | 0 | 連線池空閒 |
| Response Time Scatter | 9 條線緊密聚合，Y < 50ms | user_api 各端點延遲正常 |

> **SRE 觀察重點**：Cache Hit Rate 的爬升過程說明了「冷啟動（Cold Start）」問題。如果系統重啟後 Cache Hit Rate 長時間不回升，可能代表 Cache Key 設計問題或 TTL 設定過短。

---

### Phase 2：user_api Log Storm（scatter 散點 Y 值跳升）

**目的**：驗證「在請求路徑（request path）上執行同步 DB 寫入」對延遲的影響

**觸發動作**：程式自動呼叫 `GET http://localhost:8001/chaos/log-storm/start`

**Chaos 機制**：
- 每次請求在回應之前，先執行 50–200ms 的隨機 sleep（模擬 DB lock contention）
- 同時寫入 5 筆 `user_request_logs` 記錄（1 正常 + 4 放大）
- 因為這段邏輯在 HTTP middleware 計時範圍內，**延遲上升會直接反映在 Prometheus histogram**

**等待時間**：40 秒

#### 應觀察的指標與預期值

| Panel | 預期變化 | 代表意義 |
|-------|----------|----------|
| Response Time Scatter (P99) | 從 < 50ms 跳升至 **200ms+** | user_api 長尾請求遇到 DB 競爭延遲 |
| Response Time Scatter (P50) | 仍相對較低（50–100ms） | 多數請求只遇到部分延遲 |
| user_api_log_storm_active | 1 | Chaos 已啟動 |
| user_api_log_writes_total (rate) | 急速累積（約 RPS × 5 倍） | 每次請求寫 5 筆 log |
| Request Latency P99（主 app） | 變化不大 | 這個 Chaos 只影響 user_api |
| Cache Hit Rate | 可能略下降 | 更多請求到達（需確認） |

> **SRE 觀察重點**：注意 **P50 和 P99 的分歧（Divergence）**。P99 遠高於 P50，代表系統呈現「雙峰延遲分布」— 大多數請求正常，少數請求異常慢。這種模式在平均延遲（Mean Latency）圖表中完全看不出來，是使用百分位數的核心價值。

---

### Phase 3：主 app Log Storm（Redis/DB 面板全面反應）

**目的**：觸發主 app 的同步 Log 寫入，讓 Redis/DB 相關 Panel 全部產生可見變化

**觸發動作**：程式自動呼叫 `GET http://localhost:8000/api/anomaly/log-storm/start`

**Chaos 機制**：
- 主 app 的每次 `GET /api/data` 請求都同步寫一筆 log 到 PostgreSQL
- 主 app 和 user_api.py 共用同一個 PostgreSQL 實例，因此 DB 壓力可能**互相干擾**

**等待時間**：40 秒

#### 應觀察的指標與預期值

| Panel | 預期變化 | 代表意義 |
|-------|----------|----------|
| Log Storm Active | 跳至 **1** | 主 app chaos 已確認啟動 |
| Log Storm Write Rate | 急升（約等於主 app RPS） | 每個 /api/data 請求觸發一次 DB 寫入 |
| Log Storm Write Duration P99 | 上升（可能從 5ms → 50ms+） | DB 寫入因並發競爭而變慢 |
| DB Query Duration P99 | write type 明顯上升 | DB 整體寫入壓力上升 |
| Request Latency P99（主 app） | 上升 | 寫 log 拖慢請求回應 |
| Cache Hit Rate | 可能波動 | DB 慢時，cache miss 的回填（backfill）也會慢 |
| HTTP 5xx Error Rate | 可能出現少量 5xx | DB 超時邊緣情況 |
| Response Time Scatter (user_api P99) | 可能進一步上升 | 共用 DB，互相干擾 |

> **SRE 觀察重點**：這個 Phase 展示了**共用資源競爭（Shared Resource Contention）**問題。user_api 和 main app 共用 PostgreSQL，main app 的 Log Storm 讓 DB 更忙，進而影響 user_api 的延遲。這是微服務架構中的典型「隱性依賴（Hidden Dependency）」問題。

---

### Phase 4：DB Connection Pool Exhaustion（連線池耗盡）

**目的**：展示連線池作為關鍵資源，一旦耗盡對整個系統的影響

**觸發動作**：程式自動呼叫 `GET http://localhost:8000/api/anomaly/connection-exhaust`

**Chaos 機制**：
- 佔用連線池中 4 條連線，持續 10 秒
- 主 app 的連線池大小為 5，因此僅剩 1 條可用
- 任何新的 DB 請求都必須等待這 1 條連線，形成排隊效應（Queuing Effect）

**等待時間**：20 秒（10 秒 Chaos + 10 秒觀察恢復）

#### 應觀察的指標與預期值

| Panel | 預期變化 | 代表意義 |
|-------|----------|----------|
| DB Active Connections Held | 跳至 **4** | 4 條連線被佔用 |
| Request Latency P99 | 可能突破 **2–3 秒** | 請求在等待可用連線 |
| HTTP 5xx Error Rate | 若等待超過 timeout，出現 5xx | 連線等待超時 |
| DB Query Duration P99 | 大幅上升（包含排隊時間） | DB 看起來「很慢」，實際是在等連線 |
| 10 秒後自動恢復 | 所有指標在 30 秒內回落 | 連線釋放，排隊請求處理完畢 |

> **SRE 觀察重點**：這是所謂的 **「大爆炸（Thundering Herd）」效應** 的前兆。一旦這 4 條連線釋放，積累的等待請求同時湧入 DB，可能造成另一個短暫的延遲峰值（稱為 Recovery Spike）。注意觀察恢復後的 10 秒內是否有這個現象。

---

### Phase 5：全面恢復（Recovery）

**目的**：驗證系統在 Chaos 停止後能夠自我恢復（Self-healing）

**觸發動作**：
- `GET http://localhost:8001/chaos/log-storm/stop`
- `GET http://localhost:8000/api/anomaly/log-storm/stop`

**等待時間**：30 秒

#### 應觀察的指標與預期值

| Panel | 預期變化 | 代表意義 |
|-------|----------|----------|
| Log Storm Active | 回落至 **0** | Chaos 已確認停止 |
| Request Latency P99 | 在 30–60 秒內回到基線 | 系統恢復正常 |
| Response Time Scatter | 9 條線重新聚合，Y 值回落 | user_api 延遲恢復 |
| Cache Hit Rate | 可能短暫下降後回升 | 部分 cache 在 chaos 期間失效 |
| HTTP 5xx Error Rate | 回到 0 | 無更多錯誤 |

> **SRE 觀察重點**：**恢復速度（Recovery Time）** 是衡量系統韌性（Resilience）的重要指標，直接對應 SRE 的 MTTR（Mean Time to Recovery）概念。若系統在 60 秒內恢復，說明設計良好；若持續異常，需調查是否有 Cascading Failure（級聯故障）。

---

### Phase 6：HA Failover 示範（Patroni 自動選主）

**目的**：模擬 PostgreSQL Primary 節點機器猝死，驗證 Patroni + etcd + HAProxy 的自動 Failover 能力

**觸發動作**：程式自動偵測當前 Primary 節點後執行 `docker stop <primary-container>`

**Failover 機制（5 個步驟）**：
1. Patroni Primary 停止更新 etcd Leader Lease（Heartbeat 中斷）
2. etcd TTL(30s) 倒數 → 其餘 Patroni 節點發現 Leader 離線
3. WAL 最新的 Replica 競爭成功 → 升格為新 Primary
4. HAProxy 在下一個 check 週期（≈3s）自動重新路由寫入流量
5. 應用程式自動重連到新 Primary → 服務恢復

**等待時間**：觀察 30 秒（確認 Grafana 錯誤率回落後，重啟舊 Primary 容器使其以 Replica 身份重新加入）

#### 應觀察的指標與預期值

| Panel | 預期變化 | 代表意義 |
|-------|----------|----------|
| HTTP 5xx Error Rate | Failover 期間短暫出現（< 15 秒） | Primary 切換中斷窗口 |
| DB Active Connections | 切換瞬間歸零後恢復 | HAProxy 重新建立連線 |
| Request Latency P99 | 短暫上升後迅速回落 | 連線重試開銷 |
| HAProxy Stats (7001) | pg_write backend 切換至新 Primary | 路由切換成功 |

#### 實測結果（2026-05-31）

| 指標 | 數值 |
|------|------|
| **RTO（選主時間）** | **5 秒** |
| Failover 視窗錯誤率 | 9/5970 = **0.15%** |
| 叢集恢復時間 | < 2 分鐘（舊 Primary 以 Replica 重新加入） |
| 最終叢集狀態 | patroni3=leader(timeline:3), patroni1/2=replica |

#### 手動驗證命令

```bash
# 即時查看叢集拓撲（任意 Patroni 節點均可查詢）
curl -s http://localhost:8081/cluster | python3 -m json.tool | grep -E '"name"|"role"|"state"|"timeline"'

# HAProxy 路由狀態
curl -s http://localhost:7001/stats

# 重啟舊 Primary（它會自動以 Replica 身份加入）
docker start api_monitoring-patroni2-1
# 等待 30 秒後確認叢集狀態
```

> **SRE 觀察重點**：
> - **Split-Brain 防護**：etcd Leader Lease 機制確保網路分裂時只有一個 Primary。
> - **WAL 一致性**：只有 WAL 最新（timeline 最高）的 Replica 才能升格，避免資料遺失。
> - **自動修復**：舊 Primary 重啟後無需手動介入，Patroni 自動以 pg_basebackup 同步資料並加入叢集。
> - **MTTR 計算**：從 `docker stop` 到 HAProxy 完成路由切換 ≈ 8 秒（5s 選主 + 3s HAProxy check）。

---

## 五、測試方式二：Continuous Mode（持續模式）

適合長時間壓測或 CI/CD 整合場合。

```bash
# 正常流量（持續，Ctrl+C 停止）
python3 user_traffic_generator.py

# 指定流量強度
python3 user_traffic_generator.py --user-rps 5 --main-rps 6

# 直接以 user_api Log Storm 模式啟動（持續直到手動停止）
python3 user_traffic_generator.py --chaos log-storm-user

# 全系統 Log Storm，60 秒後自動恢復
python3 user_traffic_generator.py --chaos log-storm-all --chaos-duration 60
```

---

## 六、手動觸發 Chaos（單獨測試某個場景）

不用 traffic generator 也可以手動觸發：

```bash
# === user_api Log Storm ===
curl http://localhost:8001/chaos/log-storm/start
curl http://localhost:8001/chaos/status
curl http://localhost:8001/chaos/log-storm/stop

# === 主 app Log Storm ===
curl http://localhost:8000/api/anomaly/log-storm/start
curl http://localhost:8000/api/anomaly/log-storm/stop

# === Connection Pool Exhaustion ===
curl http://localhost:8000/api/anomaly/connection-exhaust
# 注意：此端點會自動在 10 秒後釋放連線，無需手動停止

# === 確認所有 Chaos 已清除 ===
curl http://localhost:8001/chaos/status
curl http://localhost:8000/api/anomaly/log-storm/status
```

---

## 七、指標觀察速查表（Metric Quick Reference）

### 各 Phase 指標變化矩陣

| Grafana Panel | Phase 1 Baseline | Phase 2 user_api Storm | Phase 3 全系統 Storm | Phase 4 連線耗盡 | Phase 5 恢復 | Phase 6 HA Failover |
|---------------|:---:|:---:|:---:|:---:|:---:|:---:|
| RPS | 穩定 | 穩定 | 穩定 | 可能下降 | 穩定 | 短暫下降 |
| Request Latency P50 | 低(18ms) | 微升 | **升(102ms)** | **大升(135ms)** | 回落 | 短暫上升後回落 |
| Request Latency P99 | 低(39ms) | **大升(216ms)** | 升 | **暴升(235ms)** | 回落 | 短暫上升後回落 |
| Scatter P99 (user_api) | 低 | **大升** | 持續升 | 持續升 | 回落 | 短暫上升 |
| Cache Hit Rate | 爬升 | 穩定 | 可能波動 | 可能下降 | 回升 | 穩定 |
| DB Query Duration P99 | 低 | 低 | **升** | **暴升** | 回落 | 切換瞬間歸零 |
| Log Storm Active | 0 | 0 | **1** | 1 | 0 | 0 |
| Log Storm Write Rate | 0 | 0 | **急升** | 急升 | 0 | 0 |
| DB Active Connections | 0 | 0 | 0 | **4** | 0 | 切換歸零後恢復 |
| HTTP 5xx Error Rate | 0 | 0 | 可能微升 | 可能升 | 0 | **短暫出現(0.15%)** |
| HAProxy 路由 | Primary正常 | Primary正常 | Primary正常 | Primary正常 | Primary正常 | **切換至新Primary** |

**Phase 6 實測數據（2026-05-31）**：RTO=5s, 9/5970 errors=0.15%, 最終 P50=16.2ms

> **重要**：**Phase 2 的指標只影響 user_api (port 8001)**，主 app 的 Latency / DB 面板應變化不大。這正是兩個服務分開測試的設計意圖。

---

## 八、故障排除（Troubleshooting）

### 問題：Grafana 某個 Panel 顯示 "No data"

可能原因與處理：

```bash
# 1. 確認 Prometheus 能成功 scrape 到目標
curl http://localhost:9090/api/v1/targets | python3 -m json.tool | grep -A2 "health"

# 2. 確認 user_api 的 /metrics 端點正常
curl http://localhost:8001/metrics | grep user_api_requests_total

# 3. 確認流量有在產生（應有資料才會有指標）
python3 user_traffic_generator.py  # 先讓流量跑一分鐘

# 4. 在 Prometheus 直接查詢確認指標存在
# 瀏覽器開啟：http://localhost:9090
# 輸入：user_api_requests_total
```

---

### 問題：Chaos 端點呼叫後，Grafana 沒有反應

```bash
# 確認 Chaos 狀態真的改變了
curl http://localhost:8001/chaos/status
# 應看到 "user_api_log_storm_active": true

# 確認流量有在進來（Chaos 只在有請求時才有效果）
# 同時執行流量產生器
python3 user_traffic_generator.py &

# 等待 Prometheus scrape 一次（scrape interval = 5 秒）
# Grafana 面板會在下一個 5 秒更新
```

---

### 問題：Patroni 叢集沒有 Leader / 服務無法連線 DB

```bash
# 查看叢集狀態（任意 Patroni 節點）
curl -s http://localhost:8081/cluster | python3 -m json.tool

# 若 role 全部為 replica（腦裂或 etcd 故障）：
# 查看 etcd 健康狀態
docker exec api_monitoring-etcd1-1 etcdctl --endpoints=http://localhost:2379 \
  --user=root:rootpassword cluster-health

# 強制手動 Failover（需要至少一個節點存活）
curl -X POST http://localhost:8081/failover \
  -H 'Content-Type: application/json' \
  -d '{"master": "patroni1"}'

# 重啟失效的 Patroni 容器（會自動以 Replica 加入）
docker start api_monitoring-patroni2-1
```

### 問題：HAProxy 無法路由至 Primary

```bash
# 查看 HAProxy 後端狀態
curl -s http://localhost:7001/stats | head -50

# 查看 HAProxy 日誌
docker logs api_monitoring-haproxy-1 --tail 50

# HAProxy 使用 /patroni 端點 (HTTP 200=Primary, 503=Replica) 進行健康檢查
# 若所有後端均為 DOWN，等待 Patroni 選主完成（TTL=30s）
```

---

### 問題：Connection Exhaust 後，服務沒有恢復

```bash
# 確認 DB 連線池已釋放
curl http://localhost:8001/chaos/status

# 如果主 app 的 log_storm_active 仍為 1（可能上次沒有停止）
curl http://localhost:8000/api/anomaly/log-storm/stop

# 強制重啟主 app（清除所有狀態）
docker compose restart api
```

---

## 九、測試完成後的清理（Post-test Cleanup）

```bash
# 停止流量產生器（Ctrl+C 即可，程式會自動呼叫 stop 端點）

# 確認所有 Chaos 均已停止
curl http://localhost:8001/chaos/status         # 應為 false
curl http://localhost:8000/api/anomaly/log-storm/status  # 應為 inactive

# 若需要完全重置環境
docker compose restart api
pkill -f "user_api.py"
python3 user_api.py &
```

---

## 十、延伸觀察建議（Advanced Observations）

### 觀察快取暖機（Cache Warm-up）曲線

1. 重啟服務（清空 Redis cache）
2. 開始流量
3. 在 **Cache Hit Rate** panel 觀察命中率從 0% 到穩定值的過程
4. 這個曲線的斜率代表「快取填充速度」，與查詢資料分布的 Locality 直接相關

### 觀察延遲的 P50 / P99 分歧

1. Phase 1 確認兩者接近
2. Phase 2 啟動 Chaos
3. 觀察 **Response Time Scatter** 中 P99 線與 P50 線的「開口」變大
4. 這個「開口」的大小代表系統的**延遲一致性（Latency Consistency）**

### 記錄 MTTR

1. 記錄 Phase 4 連線耗盡時的開始時間（`DB Active Connections = 4`）
2. 記錄 Phase 5 恢復後所有指標回到基線的時間
3. 時間差即為這個場景的 MTTR（Mean Time to Recovery）

---

## 附錄：常用 PromQL 查詢（可在 http://localhost:9090 直接測試）

```promql
# 當前 RPS（主 app）
rate(http_requests_total[1m])

# 當前 user_api 的 P99 延遲（所有端點合計）
histogram_quantile(0.99, sum(rate(user_api_request_duration_seconds_bucket[1m])) by (le))

# 快取命中率
100 * rate(redis_cache_hits_total[1m]) / (rate(redis_cache_hits_total[1m]) + rate(redis_cache_misses_total[1m]))

# DB 查詢 P99（依 query type 分類）
histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket[1m])) by (le, query_type))

# 過去 5 分鐘的 Availability（好請求比率）
1 - (rate(http_requests_total{http_status="500"}[5m]) / rate(http_requests_total[5m]))

# user_api Log Storm 是否啟動
user_api_log_storm_active

# 所有指標列表（搜尋 user_api 相關）
{__name__=~"user_api.*"}
```
