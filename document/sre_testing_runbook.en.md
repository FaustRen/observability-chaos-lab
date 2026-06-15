<div align="right">

**English** · [繁體中文](sre_testing_runbook.md)

</div>

# SRE Testing Runbook — API Monitoring System

> **Document scope**: This is an operational runbook for a Site Reliability Engineer (SRE) to run load tests and chaos engineering.
> **Audience**: SREs, backend engineers, platform engineers
> **Design philosophy**: Observation follows Google SRE's RED Method (Rate / Errors / Duration) and USE Method (Utilization / Saturation / Errors)

---

## 1. System Architecture Overview

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
  (runs on host)           hits port 8001 + port 8000 at once, driving every panel
```

### Service Port Cheat Sheet

| Service | Port | Purpose |
|------|------|------|
| main app (FastAPI) | `8000` | Primary API service (Docker) |
| user_api (FastAPI) | `8001` | User-simulation service (runs on host) |
| HAProxy (write) | `5432` | Routes writes to the Patroni Primary |
| HAProxy (read) | `5433` | Routes reads to the Patroni Replicas |
| HAProxy Stats | `7001` | HAProxy admin page |
| Patroni REST API | `8081/8082/8083` | Cluster status / leader-follower decision |
| Prometheus | `9090` | Metric scraping and querying |
| Grafana | `3000` | Visualization dashboards |
| Redis | `6379` | Cache layer |

---

## 2. Pre-test Checklist

Before starting any test, confirm the following items in order:

```bash
# (1) Confirm all Docker services are running (13 containers)
docker compose ps

# Expected output (all services Status = Up):
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

# (2) Confirm Patroni cluster status (there should be exactly one leader)
curl -s http://localhost:8081/cluster | python3 -m json.tool | grep role
# Expected: one "leader", two "replica"

# (3) Confirm HAProxy DB routing is healthy
curl -s http://localhost:7001/stats | grep -E 'pg_(write|read)'

# (4) Confirm user_api.py is running
curl http://localhost:8001/health
# Expected: {"status":"ok","service":"user_api","port":8001}

# (5) Confirm the main app is running
curl http://localhost:8000/

# (6) Confirm Prometheus is scraping both targets
# Open in browser: http://localhost:9090/targets
# Confirm both "user-api" and "fastapi-app" are UP

# (7) Confirm chaos state is clean (all 0 / false)
curl http://localhost:8001/chaos/status
curl http://localhost:8000/api/anomaly/log-storm/status
```

### Reset counters (recommended before each test)

Restarting services makes Prometheus counters recompute from 0, making it easier to observe changes in Grafana panels.

```bash
# Restart the main app (resets http_requests_total and other counters)
docker compose restart api

# Restart user_api.py (resets user_api_requests_total and other counters)
pkill -f "user_api.py"
python3 user_api.py &

# In Grafana, set the time range to "Last 15 minutes"
# so you only see data from the test window
```

---

## 3. System Start-up

### Step 1: Start the Docker services

```bash
cd /path/to/api_monitoring
docker compose up -d
```

### Step 2: Start user_api.py

```bash
# Option A: foreground (see logs directly, easier to debug)
python3 user_api.py

# Option B: background (does not occupy the terminal during testing)
python3 user_api.py > /tmp/user_api.log 2>&1 &

# Confirm it started successfully
curl http://localhost:8001/health
```

### Step 3: Open Grafana

1. Open `http://localhost:3000` in a browser
2. Log in: admin / `<your_password>`
3. Open the dashboard, set the time range to **Last 15 minutes**, refresh interval recommended at **5s**

---

## 4. Test Method 1: Interactive Five-Phase Chaos Demo

This is the most complete test method, suitable for **presentations** or **observing all metric changes end to end**.

```bash
python3 user_traffic_generator.py --interactive
```

Each phase has a pause point, letting you confirm the current state in Grafana before continuing.

---

### Phase 1: Normal Baseline

**Goal**: Establish a healthy baseline for all metrics

**Traffic behavior**:
- user_api.py: 60% search (`/customer/search`) / 30% compound query (`/customer/query`) / 10% register (`/customer/register`)
- main app: 70% `GET /api/data` / 30% `GET /api/write`
- Total roughly **20 RPS**

**Wait time**: 30 seconds (let the Redis cache warm up and panels stabilize)

#### Metrics to observe and expected values

| Panel | Expected value | Meaning |
|-------|--------|----------|
| HTTP Traffic - RPS | Stable, ~8-15 RPS | System is receiving traffic normally |
| Request Latency P50 | < 20ms | Most requests complete quickly |
| Request Latency P99 | < 100ms | Tail latency within an acceptable range |
| Cache Hit Rate | Gradually climbs to > 60% | Redis cache warm-up effect |
| HTTP 5xx Error Rate | 0 | No errors |
| Log Storm Active | 0 | Chaos not started |
| DB Active Connections | 0 | Connection pool idle |
| Response Time Scatter | 9 lines tightly clustered, Y < 50ms | user_api endpoint latencies normal |

> **SRE focus**: The climbing Cache Hit Rate illustrates the "cold start" problem. If the Cache Hit Rate does not recover for a long time after a restart, it may indicate a cache-key design problem or a TTL set too short.

---

### Phase 2: user_api Log Storm (scatter Y values jump up)

**Goal**: Verify the latency impact of "running synchronous DB writes on the request path"

**Trigger**: The program automatically calls `GET http://localhost:8001/chaos/log-storm/start`

**Chaos mechanism**:
- Before responding, each request first runs a random 50-200ms sleep (simulating DB lock contention)
- It also writes 5 `user_request_logs` records (1 normal + 4 amplified)
- Because this logic is inside the HTTP middleware timing scope, **the latency rise is reflected directly in the Prometheus histogram**

**Wait time**: 40 seconds

#### Metrics to observe and expected values

| Panel | Expected change | Meaning |
|-------|----------|----------|
| Response Time Scatter (P99) | Jumps from < 50ms to **200ms+** | user_api tail requests hit DB contention latency |
| Response Time Scatter (P50) | Still relatively low (50-100ms) | Most requests only hit partial latency |
| user_api_log_storm_active | 1 | Chaos started |
| user_api_log_writes_total (rate) | Accumulates rapidly (~5x RPS) | Each request writes 5 logs |
| Request Latency P99 (main app) | Little change | This chaos only affects user_api |
| Cache Hit Rate | May dip slightly | More requests arrive (confirm) |

> **SRE focus**: Note the **divergence between P50 and P99**. P99 far above P50 indicates a "bimodal latency distribution" — most requests are normal, a few are abnormally slow. This pattern is completely invisible in a mean-latency chart, which is the core value of using percentiles.

---

### Phase 3: main app Log Storm (Redis/DB panels react across the board)

**Goal**: Trigger synchronous log writes in the main app so all Redis/DB-related panels show visible change

**Trigger**: The program automatically calls `GET http://localhost:8000/api/anomaly/log-storm/start`

**Chaos mechanism**:
- Each `GET /api/data` request in the main app synchronously writes one log to PostgreSQL
- The main app and user_api.py share the same PostgreSQL instance, so the DB pressure can **interfere with each other**

**Wait time**: 40 seconds

#### Metrics to observe and expected values

| Panel | Expected change | Meaning |
|-------|----------|----------|
| Log Storm Active | Jumps to **1** | main app chaos confirmed started |
| Log Storm Write Rate | Spikes (about the main app RPS) | Each /api/data request triggers one DB write |
| Log Storm Write Duration P99 | Rises (possibly 5ms -> 50ms+) | DB writes slow down under concurrent contention |
| DB Query Duration P99 | write type rises noticeably | Overall DB write pressure rises |
| Request Latency P99 (main app) | Rises | Writing logs slows down request responses |
| Cache Hit Rate | May fluctuate | When the DB is slow, cache-miss backfill is also slow |
| HTTP 5xx Error Rate | A few 5xx may appear | DB timeout edge cases |
| Response Time Scatter (user_api P99) | May rise further | Shared DB, mutual interference |

> **SRE focus**: This phase demonstrates **shared resource contention**. user_api and the main app share PostgreSQL; the main app's Log Storm makes the DB busier, which in turn affects user_api latency. This is a classic "hidden dependency" problem in microservice architectures.

---

### Phase 4: DB Connection Pool Exhaustion

**Goal**: Show the connection pool as a critical resource and the system-wide impact once it is exhausted

**Trigger**: The program automatically calls `GET http://localhost:8000/api/anomaly/connection-exhaust`

**Chaos mechanism**:
- Holds 4 connections in the pool for 10 seconds
- The main app's pool size is 5, so only 1 remains available
- Any new DB request must wait for that 1 connection, creating a queuing effect

**Wait time**: 20 seconds (10s chaos + 10s observing recovery)

#### Metrics to observe and expected values

| Panel | Expected change | Meaning |
|-------|----------|----------|
| DB Active Connections Held | Jumps to **4** | 4 connections are held |
| Request Latency P99 | May exceed **2-3 seconds** | Requests are waiting for a free connection |
| HTTP 5xx Error Rate | 5xx appear if waits exceed timeout | Connection-wait timeouts |
| DB Query Duration P99 | Rises sharply (includes queue time) | The DB "looks slow" but is actually waiting for connections |
| Auto-recovers after 10s | All metrics settle within 30s | Connections released, queued requests processed |

> **SRE focus**: This is a precursor to the **thundering herd** effect. Once those 4 connections are released, the accumulated waiting requests flood the DB at once, possibly causing another brief latency peak (a recovery spike). Watch for this within 10 seconds after recovery.

---

### Phase 5: Full Recovery

**Goal**: Verify that the system can self-heal after chaos stops

**Trigger**:
- `GET http://localhost:8001/chaos/log-storm/stop`
- `GET http://localhost:8000/api/anomaly/log-storm/stop`

**Wait time**: 30 seconds

#### Metrics to observe and expected values

| Panel | Expected change | Meaning |
|-------|----------|----------|
| Log Storm Active | Falls back to **0** | Chaos confirmed stopped |
| Request Latency P99 | Returns to baseline within 30-60s | System back to normal |
| Response Time Scatter | 9 lines re-cluster, Y values fall | user_api latency recovers |
| Cache Hit Rate | May dip briefly then recover | Some caches invalidated during chaos |
| HTTP 5xx Error Rate | Returns to 0 | No more errors |

> **SRE focus**: **Recovery time** is an important measure of system resilience, directly mapping to SRE's MTTR (Mean Time to Recovery) concept. Recovering within 60 seconds indicates a well-designed system; persistent anomalies warrant investigating a possible cascading failure.

---

### Phase 6: HA Failover Demo (Patroni automatic leader election)

**Goal**: Simulate a sudden death of the PostgreSQL Primary node and verify the automatic failover capability of Patroni + etcd + HAProxy

**Trigger**: The program automatically detects the current Primary node and runs `docker stop <primary-container>`

**Failover mechanism (5 steps)**:
1. The Patroni Primary stops updating the etcd Leader Lease (heartbeat interrupted)
2. The etcd TTL (30s) counts down -> the remaining Patroni nodes detect the leader is offline
3. The Replica with the most recent WAL wins the race -> promoted to new Primary
4. HAProxy automatically re-routes write traffic on the next check cycle (~3s)
5. The application automatically reconnects to the new Primary -> service restored

**Wait time**: Observe for 30 seconds (after confirming the Grafana error rate has fallen, restart the old Primary container so it rejoins as a Replica)

#### Metrics to observe and expected values

| Panel | Expected change | Meaning |
|-------|----------|----------|
| HTTP 5xx Error Rate | Brief spike during failover (< 15s) | Primary switchover interruption window |
| DB Active Connections | Drops to zero at switchover, then recovers | HAProxy re-establishes connections |
| Request Latency P99 | Brief rise then quick recovery | Connection-retry overhead |
| HAProxy Stats (7001) | pg_write backend switches to new Primary | Routing switch succeeded |

#### Measured results (2026-05-31)

| Metric | Value |
|------|------|
| **RTO (leader election time)** | **5 seconds** |
| Failover-window error rate | 9/5970 = **0.15%** |
| Cluster recovery time | < 2 minutes (old Primary rejoins as Replica) |
| Final cluster state | patroni3=leader(timeline:3), patroni1/2=replica |

#### Manual verification commands

```bash
# View cluster topology in real time (any Patroni node can be queried)
curl -s http://localhost:8081/cluster | python3 -m json.tool | grep -E '"name"|"role"|"state"|"timeline"'

# HAProxy routing status
curl -s http://localhost:7001/stats

# Restart the old Primary (it automatically rejoins as a Replica)
docker start api_monitoring-patroni2-1
# Confirm cluster status after 30 seconds
```

> **SRE focus**:
> - **Split-brain protection**: The etcd Leader Lease mechanism ensures only one Primary exists during a network partition.
> - **WAL consistency**: Only the Replica with the most recent WAL (highest timeline) can be promoted, avoiding data loss.
> - **Automatic repair**: After the old Primary restarts, no manual intervention is needed; Patroni automatically syncs data via pg_basebackup and rejoins the cluster.
> - **MTTR calculation**: From `docker stop` to HAProxy completing the route switch ≈ 8 seconds (5s election + 3s HAProxy check).

---

## 5. Test Method 2: Continuous Mode

Suitable for long-running load tests or CI/CD integration.

```bash
# Normal traffic (continuous, Ctrl+C to stop)
python3 user_traffic_generator.py

# Specify traffic intensity
python3 user_traffic_generator.py --user-rps 5 --main-rps 6

# Start directly in user_api Log Storm mode (continuous until stopped manually)
python3 user_traffic_generator.py --chaos log-storm-user

# System-wide Log Storm, auto-recovers after 60 seconds
python3 user_traffic_generator.py --chaos log-storm-all --chaos-duration 60
```

---

## 6. Manually Triggering Chaos (testing a single scenario)

You can trigger chaos manually without the traffic generator:

```bash
# === user_api Log Storm ===
curl http://localhost:8001/chaos/log-storm/start
curl http://localhost:8001/chaos/status
curl http://localhost:8001/chaos/log-storm/stop

# === main app Log Storm ===
curl http://localhost:8000/api/anomaly/log-storm/start
curl http://localhost:8000/api/anomaly/log-storm/stop

# === Connection Pool Exhaustion ===
curl http://localhost:8000/api/anomaly/connection-exhaust
# Note: this endpoint automatically releases connections after 10 seconds; no manual stop needed

# === Confirm all chaos is cleared ===
curl http://localhost:8001/chaos/status
curl http://localhost:8000/api/anomaly/log-storm/status
```

---

## 7. Metric Quick Reference

### Per-phase metric change matrix

| Grafana Panel | Phase 1 Baseline | Phase 2 user_api Storm | Phase 3 System-wide Storm | Phase 4 Conn Exhaust | Phase 5 Recovery | Phase 6 HA Failover |
|---------------|:---:|:---:|:---:|:---:|:---:|:---:|
| RPS | Stable | Stable | Stable | May drop | Stable | Brief drop |
| Request Latency P50 | Low (18ms) | Slight rise | **Rise (102ms)** | **Big rise (135ms)** | Falls back | Brief rise then back |
| Request Latency P99 | Low (39ms) | **Big rise (216ms)** | Rise | **Spike (235ms)** | Falls back | Brief rise then back |
| Scatter P99 (user_api) | Low | **Big rise** | Keeps rising | Keeps rising | Falls back | Brief rise |
| Cache Hit Rate | Climbing | Stable | May fluctuate | May drop | Recovers | Stable |
| DB Query Duration P99 | Low | Low | **Rise** | **Spike** | Falls back | Drops to zero at switchover |
| Log Storm Active | 0 | 0 | **1** | 1 | 0 | 0 |
| Log Storm Write Rate | 0 | 0 | **Spikes** | Spikes | 0 | 0 |
| DB Active Connections | 0 | 0 | 0 | **4** | 0 | Zero at switchover then recovers |
| HTTP 5xx Error Rate | 0 | 0 | May rise slightly | May rise | 0 | **Brief spike (0.15%)** |
| HAProxy routing | Primary OK | Primary OK | Primary OK | Primary OK | Primary OK | **Switches to new Primary** |

**Phase 6 measured data (2026-05-31)**: RTO=5s, 9/5970 errors=0.15%, final P50=16.2ms

> **Important**: **Phase 2 metrics only affect user_api (port 8001)**; the main app's Latency / DB panels should change little. This is exactly the design intent of testing the two services separately.

---

## 8. Troubleshooting

### Problem: a Grafana panel shows "No data"

Possible causes and handling:

```bash
# 1. Confirm Prometheus can successfully scrape the target
curl http://localhost:9090/api/v1/targets | python3 -m json.tool | grep -A2 "health"

# 2. Confirm the user_api /metrics endpoint works
curl http://localhost:8001/metrics | grep user_api_requests_total

# 3. Confirm traffic is being generated (metrics only appear when there is data)
python3 user_traffic_generator.py  # let traffic run for a minute first

# 4. Query directly in Prometheus to confirm the metric exists
# Open in browser: http://localhost:9090
# Enter: user_api_requests_total
```

---

### Problem: after calling a chaos endpoint, Grafana shows no reaction

```bash
# Confirm the chaos state actually changed
curl http://localhost:8001/chaos/status
# Should show "user_api_log_storm_active": true

# Confirm traffic is coming in (chaos only has effect when there are requests)
# Run the traffic generator at the same time
python3 user_traffic_generator.py &

# Wait for one Prometheus scrape (scrape interval = 5 seconds)
# Grafana panels update on the next 5-second cycle
```

---

### Problem: Patroni cluster has no leader / services cannot connect to the DB

```bash
# View cluster status (any Patroni node)
curl -s http://localhost:8081/cluster | python3 -m json.tool

# If all roles are replica (split-brain or etcd failure):
# Check etcd health
docker exec api_monitoring-etcd1-1 etcdctl --endpoints=http://localhost:2379 \
  --user=root:rootpassword cluster-health

# Force a manual failover (requires at least one surviving node)
curl -X POST http://localhost:8081/failover \
  -H 'Content-Type: application/json' \
  -d '{"master": "patroni1"}'

# Restart the failed Patroni container (it automatically rejoins as a Replica)
docker start api_monitoring-patroni2-1
```

### Problem: HAProxy cannot route to the Primary

```bash
# View HAProxy backend status
curl -s http://localhost:7001/stats | head -50

# View HAProxy logs
docker logs api_monitoring-haproxy-1 --tail 50

# HAProxy uses the /patroni endpoint (HTTP 200=Primary, 503=Replica) for health checks
# If all backends are DOWN, wait for Patroni leader election to complete (TTL=30s)
```

---

### Problem: after Connection Exhaust, the service does not recover

```bash
# Confirm the DB connection pool has been released
curl http://localhost:8001/chaos/status

# If the main app's log_storm_active is still 1 (it may not have stopped last time)
curl http://localhost:8000/api/anomaly/log-storm/stop

# Force-restart the main app (clears all state)
docker compose restart api
```

---

## 9. Post-test Cleanup

```bash
# Stop the traffic generator (Ctrl+C; the program automatically calls the stop endpoints)

# Confirm all chaos has stopped
curl http://localhost:8001/chaos/status         # should be false
curl http://localhost:8000/api/anomaly/log-storm/status  # should be inactive

# To fully reset the environment
docker compose restart api
pkill -f "user_api.py"
python3 user_api.py &
```

---

## 10. Advanced Observations

### Observe the cache warm-up curve

1. Restart services (flush the Redis cache)
2. Start traffic
3. Watch the **Cache Hit Rate** panel as the hit rate goes from 0% to its steady value
4. The slope of this curve represents the "cache fill rate", directly related to the locality of the queried data distribution

### Observe the P50 / P99 latency divergence

1. Phase 1: confirm the two are close
2. Phase 2: start chaos
3. Watch the "gap" between the P99 line and the P50 line widen in the **Response Time Scatter**
4. The size of this "gap" represents the system's **latency consistency**

### Record MTTR

1. Record the start time of Phase 4 connection exhaustion (`DB Active Connections = 4`)
2. Record the time when all metrics return to baseline after Phase 5 recovery
3. The difference is the MTTR (Mean Time to Recovery) for this scenario

---

## Appendix: Common PromQL Queries (test directly at http://localhost:9090)

```promql
# Current RPS (main app)
rate(http_requests_total[1m])

# Current user_api P99 latency (all endpoints combined)
histogram_quantile(0.99, sum(rate(user_api_request_duration_seconds_bucket[1m])) by (le))

# Cache hit rate
100 * rate(redis_cache_hits_total[1m]) / (rate(redis_cache_hits_total[1m]) + rate(redis_cache_misses_total[1m]))

# DB query P99 (grouped by query type)
histogram_quantile(0.99, sum(rate(db_query_duration_seconds_bucket[1m])) by (le, query_type))

# Availability over the last 5 minutes (ratio of good requests)
1 - (rate(http_requests_total{http_status="500"}[5m]) / rate(http_requests_total[5m]))

# Whether the user_api Log Storm is active
user_api_log_storm_active

# List all metrics (search for user_api-related)
{__name__=~"user_api.*"}
```
