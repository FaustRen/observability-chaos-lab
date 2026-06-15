<div align="right">

**English** · [繁體中文](README.zh-TW.md)

</div>

# API Monitoring · Backend Observability & Chaos Engineering Lab

A fully self-contained backend system that runs locally and demonstrates an end-to-end implementation of **High Availability (HA)**, **RED-method Observability**, and **Chaos Engineering**. The environment is composed of **18 Docker containers + 1 host service**, and can be started with a single command to inject faults, observe metrics, and verify automatic failover.

---

## ✨ Highlights

| Aspect | Description |
|---|---|
| **Dual High Availability** | Both PostgreSQL (Patroni + etcd Raft + HAProxy) and Redis (Sentinel) support automatic failover |
| **Hybrid Storage** | Cache-Aside pattern: Redis cache + PostgreSQL persistence layer |
| **Full Observability** | Prometheus scraping + Grafana dashboards across a four-tier chain: HTTP → Cache → DB → System |
| **Chaos Engineering** | Multiple fault-injection endpoints (latency, errors, connection-pool exhaustion, cache flush, node down, etc.) |
| **Layered OOP Design** | Clear separation of Router / Repository / Metrics / Config, with an App Factory entry point |

---

## 🏗️ System Architecture

```
                              ┌──────────────┐
        Observability         │  Grafana     │  :3000
                              │  Prometheus  │  :9090
                              └──────┬───────┘
                                     │ scrape /metrics
                              ┌──────┴───────┐
        Application           │  FastAPI     │  :8000   ← Cache-Aside business logic
                              └──┬────────┬──┘
                  Sentinel HA    │        │   Patroni HA
              ┌──────────────────┘        └──────────────────┐
        ┌─────┴──────┐                              ┌─────────┴────────┐
        │ Redis      │ Master + 2 Replica           │ HAProxy          │ :5432 write / :5433 read
        │ + 3 Sentinel                              │ Patroni ×3       │
        └────────────┘                              │ etcd ×3 (Raft)   │
                                                    └──────────────────┘
```

See [document/project_wiki.pdf](document/project_wiki.pdf) and [document/system_architecture.pdf](document/system_architecture.pdf) for detailed diagrams and explanations.

---

## 🧱 Tech Stack

- **Application framework**: FastAPI (Python 3.x)
- **Cache HA**: Redis Master/Replica + Redis Sentinel (quorum=2)
- **Database HA**: PostgreSQL + Patroni + etcd (Raft consensus) + HAProxy
- **Observability**: Prometheus + Grafana + redis_exporter + postgres_exporter
- **Orchestration**: Docker Compose (with healthcheck dependency chain)

---

## 🚀 Quick Start

### Prerequisites
- Docker Desktop (running)
- Python 3.x, with `requests` installed: `pip install requests`

### Start
```bash
docker compose up -d        # start all 18 containers
docker compose ps           # verify all are Up / healthy
```

### Verify
| Service | Address |
|---|---|
| API health check | http://localhost:8000/ |
| Prometheus metrics | http://localhost:8000/metrics/ |
| Prometheus UI | http://localhost:9090 |
| Grafana | http://localhost:3000 (admin / password in `.env`) |

> Optional host-side demo service: `python3 user_api.py` → http://localhost:8001/docs

For the full startup, dashboard import, and testing flow, see [document/usage.txt](document/usage.txt).

---

## 🔌 API Endpoints

### Business endpoints
| Method | Path | Description |
|---|---|---|
| GET | `/data` | Cache-Aside read (check Redis first, fall back to PostgreSQL on a miss and backfill) |
| GET | `/write` | Write data and invalidate the cache |

### Chaos-injection endpoints (`/anomaly/*`)
| Path | Simulated scenario |
|---|---|
| `/lag` | Inject response latency |
| `/error` | Inject application-layer error |
| `/db-overload` | Database high load |
| `/db-error` | Database error |
| `/cache-flush` | Flush the Redis cache |
| `/redis-down` | Simulate Redis master going down (triggers Sentinel failover) |
| `/connection-exhaust` | Connection-pool exhaustion anti-pattern |
| `/log-storm/{start,stop,status}` | Log-storm toggle and status |

---

## 📊 Monitoring & Dashboards

`grafana_dashboard.json` uses the Grafana v2 API format and must be imported via command line (not the UI Import).
Import steps are detailed in [document/usage.txt](document/usage.txt). The dashboard covers:

- **RED Method**: Rate / Errors / Duration (including P99)
- **Cache hit ratio and latency**
- **Database connections, query latency, and failover status**
- **System resources (CPU / Memory)**

---

## 📸 Live Demo — Dashboard Results

The screenshots below were captured live from Grafana while running the chaos runbook end-to-end. Each one corresponds to a phase in [document/sre_testing_runbook.en.md](document/sre_testing_runbook.en.md).

### 1. Baseline (Steady State)
Normal traffic with a healthy cache hit ratio and no errors — the reference point for every fault that follows.

![Baseline dashboard](document/screenshots/01_baseline.png)

### 2. user_api Log Storm
A log storm is triggered on the host `user_api` service, driving up its write rate and latency while the rest of the system stays stable.

![user_api log storm](document/screenshots/02_user_storm.png)

### 3. System-wide Log Storm + DB Errors
The storm spreads across the stack and DB errors are injected, lighting up the chaos indicators and 5xx error panels.

![System-wide log storm and DB errors](document/screenshots/03_system_storm.png)

### 4. Connection-Pool Exhaustion
The DB connection pool is deliberately exhausted (18/20 connections held), pushing query latency P99 up and surfacing active-connection saturation.

![Connection-pool exhaustion](document/screenshots/04_conn_exhaust.png)

### 5. Recovery
Fault injection stops; latency, errors, and connection usage return to baseline as the system self-heals.

![Recovery](document/screenshots/05_recovery.png)

### 6. PostgreSQL HA Failover
The primary PostgreSQL node is killed. A brief 5xx/503 spike and latency surge appear while Patroni elects a new leader (RTO ≈ 5s).

![HA failover](document/screenshots/06_ha_failover.png)

### 7. HA Recovered
The old primary rejoins as a streaming replica and the newly elected leader serves traffic — errors back to zero, cluster healthy again.

![HA recovered](document/screenshots/07_ha_recovered.png)

---

## 🧪 Chaos & Load-Testing Scripts

| Script | Purpose |
|---|---|
| `chaos_scenario.py` | Orchestrate chaos scenarios by triggering fault-injection endpoints in sequence |
| `traffic_generator.py` | Generate background traffic against the API |
| `user_api.py` / `user_traffic_generator.py` | Host-side demo service and its traffic generator |

For the full chaos and failover test runbook, see [document/sre_testing_runbook.md](document/sre_testing_runbook.md).

---

## 📁 Project Structure

```
api_monitoring/
├── app/                     # FastAPI application (layered OOP)
│   ├── main.py              # App Factory entry (lifespan / middleware / router assembly)
│   ├── config.py            # Environment-variable settings (immutable Settings)
│   ├── metrics.py           # Prometheus metric definitions
│   ├── repositories/        # Data-access layer (cache_repo / item_repo)
│   └── routers/             # Routing layer (items / anomaly)
├── haproxy/                 # HAProxy config (read/write split)
├── patroni/                 # Patroni node config
├── prometheus/              # Prometheus scrape config
├── docker-compose.yml       # 18-container orchestration
├── grafana_dashboard.json   # Grafana dashboard definition
├── chaos_scenario.py        # Chaos scenario script
├── traffic_generator.py     # Traffic generator
└── document/                # Architecture docs, slides, and test runbook (PDF)
```

---

## 📚 Further Documentation

| Document | English | 繁體中文 |
|---|---|---|
| Project Wiki | [PDF](document/project_wiki.en.pdf) | [PDF](document/project_wiki.pdf) |
| Presentation Slides | [PDF](document/presentation_slides.en.pdf) | [PDF](document/presentation_slides.pdf) |
| SRE Testing Runbook | [Markdown](document/sre_testing_runbook.en.md) | [Markdown](document/sre_testing_runbook.md) |
| Startup & Usage Guide | [Text](document/usage.en.txt) | [Text](document/usage.txt) |

Diagrams (language-neutral):
- [System Architecture (PDF)](document/system_architecture.pdf)
- [Class Diagram (PDF)](document/class_diagram.pdf)
- [Chaos Scenario Sequence Diagram (PDF)](document/Sequence_Chaos.pdf)

---

## 📄 License

Released under the [MIT License](LICENSE).
