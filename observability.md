I have a complete, evidence-backed picture. No files were modified.

---

# ConfessIt V2 — Observability Stack Audit

## 1. Architecture / data flow (as actually wired)

```
                                    ┌──────────────────────────────────────────────┐
                                    │  Grafana 10.4.3   127.0.0.1:3050            │
                                    │  datasources: []   dashboards: []   ← EMPTY  │
                                    └───▲──────────────▲──────────────▲────────────┘
                     nothing wired    │              │              │
                                    (none)      (none)         (none)
 ┌────────────────┐           ┌────────┴──────────────┴──────────────┴────────┐
 │  FastAPI       │  stdout   │            Prometheus  v2.52.0                 │
 │  backend       ├──────────►│  127.0.0.1:9090                                │
 │  :8001 (0.0.0.0)│          │  scrape jobs: prometheus, otel-collector:8889  │
 │                │          │  → 8889 returns EMPTY body (0 samples)          │
 │ _init_tracing  │  OTLP    └───────▲──────────────────────────┬─────────────┘
 │ FastAPIInstr.  │ gRPC 4317         │ scrapes                  │ (no 8888 job)
 │ LoggingHandler ├──────────►┌────────┴──────────────────────────┴─────────────┐
 └───────┬────────┘           │  otel-collector-contrib 0.99.0 (internal only) │
         │                    │  :4317 gRPC  :4318 HTTP  :8889 prom  :8888 self │
         │ OTLP logs          │  pipelines: logs | metrics | traces            │
         │ (INFO only)        └───┬──────────────┬───────────────┬─────────────┘
         │                        │ loki exporter│ prom exporter │ otlp/tempo
         │                        │ X-Scope-OrgID│  (DEAD)       │ :4317
         ▼                        ▼              ▼               ▼
 ┌───────────────┐   ┌──────────────────┐  (empty)   ┌────────────────────┐
 │ promtail      │   │ Loki 2.9.8       │            │ Tempo 2.5.0        │
 │ :9080         │   │ 127.0.0.1:3100   │            │ 127.0.0.1:3200     │
 │ scrape_configs│──►│ auth_enabled     │            │ local store /var/  │
 │  ① docker     │   │ retention 168h   │            │ tempo, block_ret   │
 │    /var/lib/  │   │ ruler → :9093 ☠  │            │ 24h                │
 │    docker/…   │   │ compactor ✓      │            │ metrics-generator  │
 │  ② backend    │   └──────────────────┘            │ NOT configured ☠   │
 │    /var/log/  │                                  └────────────────────┘
 │    confessit/ │◄─ mounts ALL of ./backend (incl. .env)
 └───────────────┘
        ▲
        └── ① scrapes the ENTIRE Docker host: 14 containers incl.
            4 belonging to OTHER projects (timetable-*, voucherbot-*)
```

**Three separate paths carry the same backend log line:**

| Path | Mechanism | Loki `job` | Carries uvicorn logs? | Carries app logs? |
|---|---|---|---|---|
| Docker stdout | promtail `docker` glob | `docker` | **yes** (3011 lines) | yes (3075) |
| `my_app.log` file | promtail `backend-file` glob | `backend-file` | no | yes (7087) |
| OTLP | `LoggingHandler` → collector → loki exporter | `confessit-backend` | **no** | yes (298) |

---

## 2. Working and verified

| Component | Status | How verified |
|---|---|---|
| All 10 containers up | ✅ | `docker compose ps` |
| Backend → collector OTLP gRPC | ✅ | Collector self-telemetry: `otelcol_receiver_accepted_spans{transport="grpc"} 2034`, `…log_records 297` |
| Backend → collector → **Tempo** | ✅ | `otelcol_exporter_send_failed_spans{exporter="otlp/tempo"} 0`; fetched trace `cdcc8586…` by ID, 3 spans with full HTTP attributes |
| Backend → collector → **Loki** (OTLP logs) | ✅ | `otelcol_exporter_send_failed_log_records{exporter="loki"} 0`; LogQL `{job="confessit-backend"}` returns records |
| **promtail → Loki** (file + docker) | ✅ | 8703 lines `job=docker`, 7672 lines `job=backend-file` |
| Loki auth (`X-Scope-OrgID`) | ✅ | `tenant_id: confessit` set; `auth_enabled: true`; queries succeed with header |
| **Trace/log correlation exists** | ⚠️ partial | OTLP log body carries `"traceid":"…","spanid":"…"`; that ID resolves in Tempo. But **not** as Loki labels |
| Docker log mount on Windows | ✅ | promtail sees 14 `*-json.log` files under `/var/lib/docker/containers` (WSL2-backed) |
| Loki compactor + retention | ✅ | Logs: `applying retention with compaction` every ~11 min; `/loki/compactor/{retention,deletion}` present |
| UIs loopback-only | ✅ | `Test-NetConnection` on LAN IP `172.17.80.1`: 3050/3100/3200/9090 all **False** |
| Redis / OTLP ports internal | ✅ | `6379/tcp`, `4317-4318/tcp`, `8889/tcp` — no host mapping |
| Secrets not in git | ✅ | `git ls-files` fails for `.env`, `backend/.env`, `db_password.txt`; all in `.gitignore` |
| Secrets not baked into image | ✅ | `backend/.dockerignore` excludes `.env` and `my_app.log*`; Dockerfile ends in `COPY . .` |

**Net effect: telemetry is collected and stored correctly. Nothing is *readable* through Grafana, and there are no application metrics at all.**

---

## 3. Problems

### CRITICAL

**C1 — Grafana has zero datasources and zero dashboards. The entire read surface is non-functional.**
Verified: authenticated `GET /api/datasources` → `[]`; `GET /api/search?type=dash-db` → `[]`; `/etc/grafana/provisioning/{datasources,dashboards}` both empty. `docker-compose.yml:128-149` mounts only `grafana_data` — **no provisioning volume**. `README.md:58-60` documents datasource setup as a *manual one-time step*, so a fresh `docker compose up` yields a blank Grafana and the config is unreproducible/drift-prone.
> Impact: every other component in the stack is currently unreachable through its intended UI.

**C2 — Prometheus contains zero application metrics. The metrics pipeline is entirely dead.**
Verified three independent ways:
- `GET /api/v1/label/__name__/values` → 228 names, **all** `prometheus_*`/`go_*`/`process_*`/`scrape_*`/`net_*`. Zero `http_server_*`, `target_info`, `custom_*`.
- `scrape_samples_scraped{job="otel-collector"}` = **0** while `up{job="otel-collector"}` = **1**.
- Raw `http://otel-collector:8889/metrics` returns a **0-line body**.

Root cause: `observability/otel-collector-config.yml:26-28` wires a `metrics` pipeline from an `otlp` receiver, but **no `MeterProvider` is ever created in the backend** — `grep` for `MeterProvider|create_counter|create_histogram|OTLPMetricExporter|prometheus_client` across `backend/**/*.py` returns **nothing** (only match for `Instrumentor` is `FastAPIInstrumentor` at `main.py:26,239`). The receiver accepts zero metric points (no `otelcol_receiver_accepted_metric_points` series exists at all).
> Impact: the "metrics" leg of the three pillars does not exist. No RED, no request rate, no latency, no error ratio. Prometheus only observes itself.

**C3 — Live magic-link authentication tokens are written to logs at INFO, then stored in Loki for 7 days.**
Verified: `backend/app/services/auth_service.py:220` → `logger.info(f"Magic Link for {user.name}: {magic_link}")`. `backend/my_app.log` lines 77, 84, 100, 101, 102, 128, 173, 174 contain e.g.
`Magic Link for Test User: http://localhost:5173/verify-login?token=hLIP38yMl0Mjr5CcI4IxkPXK8xgvV2A7IT1mHQ1HiEk`
These reach Loki via both the `backend-file` and `docker` jobs, and via OTLP. `observability/loki-config.yml:35` sets `retention_period: 168h`.
> Impact: anyone with host access, or Grafana/Loki read access, can impersonate any user. The `login_tokens` table stores only a *hash* (`DATA_HANDLING_REPORT.md:13`), so the plaintext token exists **only** in logs — the one place it must not be.

**C4 — Full query strings, including asset-delivery bearer tokens, are recorded as span attributes in Tempo.**
Verified empirically: I issued `GET /?t=SECRET_ASSET_TOKEN_ABC&password=hunter2`, pulled the `traceid` out of the OTLP log line, fetched that trace from Tempo, and got:
```
http.url = http://172.20.0.9:8001/?t=SECRET_ASSET_TOKEN_ABC&password=hunter2
```
`backend/app/main.py:310,323` accept the scoped asset token as a **query parameter** (`t: Optional[str] = Query(...)`), explicitly because `<img src>` cannot send a header (`main.py:256-258`). `http.target` is correctly query-stripped, but `http.url` is not.
> Impact: every profile-picture and love-note image view writes a valid, unexpired scoped bearer credential into the trace store, alongside any other query parameter a client sends.

### HIGH

**H1 — Every backend log line is ingested into Loki three times.** Verified by timestamp alignment of the same event: OTLP `1790497362049794304` / docker `1790497362102889683` (Δ53 ms) / backend-file for `Root endpoint accessed`. `backend/app/logger.py:80-84` attaches a console handler, a file handler, and an OTLP handler to the same logger, while `docker-compose.yml:167` mounts the same file promtail already reads via the docker glob.
> Impact: ~3× storage, and every naive LogQL query returns triplicated lines. Verified count for one message over 24h: `docker` 1840 vs `confessit-backend` 8.

**H2 — promtail ships the entire Docker host's logs, including four unrelated projects' containers.** `observability/promtail-config.yml:19` globs `/var/lib/docker/containers/*/*-json.log` with no filtering and no `docker_sd_configs`. Verified: 14 container IDs are ingested, of which 4 are not this project — `timetable-frontend`, `timetable-backend`, `timetable-postgres`, `voucherbot-postgres` (matched against `docker ps`).
> Impact: another application's logs — potentially its users' data and its own connection strings — are copied into ConfessIt's Loki. Nothing in the label set distinguishes them (see H6).

**H3 — promtail mounts the entire `backend/` directory, including the secrets file.** `docker-compose.yml:167` mounts `./backend:/var/log/confessit:ro`, and `backend/.env` contains `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `CLOUDINARY_API_SECRET`, `CLOUDINARY_URL` (key names verified; values not printed). Verified present inside the container at `/var/log/confessit/.env`.
> Impact: **not currently exfiltrated** — the glob is `*.log*` (`promtail-config.yml:26`), which does not match `.env`. But the entire secret-bearing tree is handed to a container whose only job is to read files and ship them. A one-character glob change (`*`) exfiltrates every credential.

**H4 — Tempo's metrics-generator is disabled, so no service graphs and no RED metrics from traces.** Verified: `docker logs tempo` → `level=warn … msg="metrics-generator is not configured." err="no metrics_generator.storage.path configured"`. `observability/tempo.yml` has no `metrics_generator` block, and no remote-write to Prometheus. `/api/metrics/query_range` and `/api/metrics/summary` are served but have no data source.
> Impact: trace-derived RED metrics are unavailable. Combined with C2, there is no metrics path at all.

**H5 — Traces contain no child spans. No DB, Redis, or outbound-HTTP instrumentation.** Verified: every trace inspected (`81bb49c4…`, `cdcc8586…`) has exactly 3 spans, all from `FastAPIInstrumentor`/ASGI (`GET /` SERVER + two `http send` INTERNAL). `backend/requirements.txt` has no `opentelemetry-instrumentation-sqlalchemy`, `-psycopg`, `-redis`, or `-requests/-httpx`. The app uses raw SQLAlchemy/psycopg (`app/postgres.py`), Redis pub/sub (`app/services/redis_service.py`), and Cloudinary over HTTP.
> Impact: traces answer "how long did the request take" and nothing about "what did it do". A slow query is indistinguishable from a slow handler.

**H6 — The `docker` job has no usable labels.** Verified: Loki's entire label set is `["exporter","filename","job","level"]`. There is no `container`, `service`, or `stream` label. Streams are keyed by `filename` = the ephemeral 64-hex container ID, so **every `docker compose up` that recreates a container opens a brand-new stream set and orphans the old one**. Verified from `docker logs promtail`: the `docker` job's lines are the raw `{"log":…,"stream":…,"time":…}` JSON envelope with `stream` (stdout/stderr) buried in the body.
> Impact: H2's foreign containers and this project's own containers are indistinguishable in the UI except by cross-referencing a container-ID hash. This also creates a **feedback loop**: verified that Loki's own query-access logs (which embed the full LogQL query text) are ingested into Loki, so every query writes new logs.

**H7 — Zero alerting, and dead alerting config.** Verified: `GET /api/v1/rules` → `{"groups":[]}`; `GET /api/v1/alertmanagers` → `{"activeAlertmanagers":[]}`; no Alertmanager container exists. `observability/loki-config.yml:42-43` sets `ruler: alertmanager_url: http://localhost:9093` with no `rule_files` and an empty `/loki/rules` — a ruler that cannot alert and points at a port nothing listens on. `observability/prometheus.yml` has no `rule_files` and no alerting block.

**H8 — PostgreSQL is published to the LAN.** `docker-compose.yml:64-65` uses `ports: - "5432:5432"` (0.0.0.0) while every observability UI is deliberately loopback-bound. Verified: `Test-NetConnection 172.17.80.1:5432` → **True** (contrast 3050/3100/3200/9090 → all False).
> Impact: inconsistent with the file's own stated security posture; the DB is the highest-value target in the stack.

### MEDIUM

**M1 — Trace IDs are not queryable as Loki labels.** Verified: the OTLP stream's labels are exactly `{"exporter":"OTLP","job":"confessit-backend","level":"INFO"}`. `traceid`/`spanid` exist only inside the JSON body. Correlation therefore requires `| json | traceid="…"` rather than `{trace_id="…"}`, and the `docker`/`backend-file` copies carry **no trace ID at all** (plain text lines). So the two paths you'd actually correlate from disagree.

**M2 — The three log paths carry different content.** Verified over 24h: `"Uvicorn running"` → `docker` 3011, `backend-file` **0**, `confessit-backend` **0**. Uvicorn's lifecycle/access logs exist *only* on the stdout path. Separately, `backend/app/logger.py:71` sets `LoggingHandler(level=logging.INFO)` while `logger.py:76` sets the logger to `DEBUG`, so any future DEBUG record would reach the file and console but be **silently dropped** from OTLP.

**M3 — promtail re-ingests rotated log archives.** `observability/promtail-config.yml:26` globs `*.log*`, matching `my_app.log.2025-08-22` … `my_app.log.2026-09-26` (11 rotated files, verified in Loki's `filename` values). Verified churn in `docker logs promtail`: repeated `skipping update of position for a file which does not currently exist` / `stopped tailing file` for `my_app.log.2025-08-23`, `2025-10-25`, `2025-10-30`. A year of history is re-ingested whenever positions or the Loki volume are lost, restarting the 7-day retention clock with stale data.

**M4 — The collector's own telemetry is never scraped.** The collector serves self-metrics on `:8888` (verified: `otelcol_receiver_accepted_spans 2034`, `otelcol_exporter_send_failed_* 0`) and the app-facing exporter on `:8889`. `observability/prometheus.yml:10-12` scrapes only `8889`, which is empty. The single most useful health signal in the stack — proof that pipelines are moving data — is invisible to Prometheus.

**M5 — Stale OTel semantic conventions.** Verified span attributes: `http.method`, `http.status_code`, `http.flavor`, `http.target`, `net.peer.ip` — the pre-1.20 convention, from `opentelemetry-instrumentation-fastapi==0.46b0` (`backend/requirements.txt:22`). Current convention is `http.request.method` / `http.response.status_code` / `network.protocol.version` / `client.address`.
> Impact: modern Tempo span metrics, service graphs, and Grafana's default Tempo panels key off the new names and will not match.

**M6 — Container restarts are not coordinated by health.** `docker-compose.yml:112-114, 169-170` use bare `depends_on` for `otel-collector`→`loki`/`tempo` and `promtail`→`loki`. No `healthcheck:` exists anywhere in the file. A slow Loki start produces exporter failures at boot (currently masked because `BatchSpanProcessor` retries).

**M7 — `nginx/` is an empty directory** and the backend publishes `0.0.0.0:8001` directly (`docker-compose.yml:36-37`, verified reachable on the LAN). There is no reverse proxy, so no access logging, no request-ID header, and no single place to attach trace context at the edge.

### LOW

- **L1** — `observability/tempo.yml` leaves `usage_report.reporting_enabled: true` (verified in `/status/config`) — Tempo phones home to Grafana.
- **L2** — `backend/requirements.txt:23` installs `opentelemetry-instrumentation-logging==0.46b0` but it is **never imported**. It is exactly the package that would inject trace context into records; its absence is why M1 needs body parsing.
- **L3** — `docker-compose.yml:146` `GF_AUTH_ANONYMOUS_ORG_ROLE: ""` is a no-op when `GF_AUTH_ANONYMOUS_ENABLED: "false"` (the preceding line) already governs it.
- **L4** — `docker-compose.yml:89` `--maxmemory-policy allkeys-lru` on a Redis used purely for pub/sub is inert (pub/sub keys are never persisted) and misleading.
- **L5** — `backend` runs `uvicorn --reload` inside a container (`docker-compose.yml:26-27`). This double-processes startup, duplicates lifespan events, and is dev-only behaviour in what is otherwise a production-shaped compose file.
- **L6** — Tempo's `/api/search` returns `{"traces":[]}` while `inspectedTraces: 345` (verified). Recent traces live only in the ingester and are excluded by `query_backend_after: 15m0s`. Trace *search* looks broken; trace *lookup by ID* works. Expect user confusion.
- **L7** — No frontend telemetry at all. `frontend/package.json` has no OTel/Sentry/analytics dependency (verified across all 45 deps), so there is no browser-side RUM, no JS error reporting, and no client→backend trace linkage.

---

## 4. Verified vs. suspected

**Verified by direct observation** (querying the live system): C1, C2, C3, C4, H1, H2, H3 (mount present + glob does not currently match), H4, H5, H6, H7, H8, M1, M2, M3, M4, M5, L1, L2, L6. All "Working and verified" rows.

**Inferred from configuration, not exercised:** M6 (healthcheck ordering would only misbehave under a slow start), L5 (`--reload` in a container — I confirmed the flag but did not observe duplicate lifespan side effects), L7 (absence is verified; the consequence is inferred), M2's DEBUG-drop path (verified that the levels differ; no DEBUG records exist today — 0 found in `my_app.log`).

**Not assessed:** frontend runtime behaviour, whether `archives/` (`app/services/message_archival_service.py`) writes user message content to disk that is *not* currently scraped (I confirmed `./archives` is mounted into the backend at `/app/archives` and is **not** under any promtail glob — so this is currently safe, but it is one path change away from H3's failure mode).

---

## 5. Prioritized recommendations

**P0 — restore basic function (the stack is currently unreadable and unmeasured)**
1. Provision Grafana datasources as files under a mounted `observability/grafana/provisioning/datasources/` — Prometheus (`prometheus:9090`), Loki (`http://loki:3100`, `X-Scope-OrgID: confessit`), Tempo (`http://tempo:3200`). Fixes C1 and removes the manual README step.
2. Add a `MeterProvider` with a `PeriodicExportingMetricReader` (OTLP) in `backend/app/main.py`, and instrument `opentelemetry-instrumentation-asgi`/FastAPI + SQLAlchemy + Redis. Then the existing `metrics` pipeline (`otel-collector-config.yml:26-28`) lights up and `8889` stops serving an empty body. Fixes C2, and with `opentelemetry-instrumentation-sqlalchemy` also H5.
3. Scrape `otel-collector:8888` as a third Prometheus job. One line in `prometheus.yml`; immediately gives you pipeline health and export-failure counters. Fixes M4.

**P1 — stop leaking credentials (do before any shared access to Grafana)**
4. Delete the token from `auth_service.py:220`; log only the recipient and a truncated hash, or nothing. Fixes C3.
5. Add an OTel span processor in `backend/app/main.py` that drops or rewrites `http.url` query strings (a `SpanProcessor` setting `http.url` to the path only, and hashing/redacting `t`). Fixes C4.
6. Narrow the promtail backend mount from `./backend` to the log file only, or move logs to a dedicated `./observability/logs` directory. Fixes H3 structurally rather than by relying on the glob.
7. Change the `docker` scrape to a relabeled, allow-listed form, or drop the `docker` job entirely and rely on the OTLP path. Fixes H2 and H6 together and eliminates the Loki self-query feedback loop.

**P2 — correctness and cost**
8. Pick one ingestion path. Given OTLP already carries trace-correlated structured logs, the cheapest fix is to drop the `backend-file` job and stop writing `my_app.log` (`logger.py:24`), or at minimum drop the console handler so stdout is not double-shipped. Fixes H1, M2, M3.
9. Promote `traceid` to a Loki label in the collector's `loki` exporter, and add `service_name` as a label. Fixes M1.
10. Configure Tempo `metrics_generator` with a `remote_write` to Prometheus. Fixes H4 and gives RED from traces even before P0#2 lands.
11. Change `docker-compose.yml:64-65` to `127.0.0.1:5432:5432`, matching the file's own stated posture. Fixes H8.
12. Install `LoggingInstrumentor` (already a dependency) or add a `LoggingHandler` filter, and pin the OTLP handler level to match the logger level. Fixes the remaining half of M2 and retires L2.

**P3 — operability**
13. Add `healthcheck:` + `depends_on: condition: service_healthy` for loki/tempo/prometheus. Fixes M6.
14. Either add Prometheus `rule_files` plus an Alertmanager, or delete the `ruler` block and the `alertmanager_url` from `loki-config.yml`. Right now both alerting paths are decorative. Fixes H7.
15. Add `opentelemetry-instrumentation-redis` and an `httpx`/requests instrumentor for Cloudinary calls; refresh `opentelemetry-instrumentation-fastapi` to a current version for modern semconv. Fixes H5, M5.
16. Provision a minimal dashboard set (Golden Signals + a trace-to-log panel) once datasources exist.
17. Set `usage_report.reporting_enabled: false` in `tempo.yml`; drop `--reload` from the compose command; remove the inert Redis maxmemory policy and the `GF_AUTH_ANONYMOUS_ORG_ROLE` line. Fixes L1, L3, L4, L5.

---

**Bottom line:** the transport layer is genuinely well built and healthy — ingestion works end to end for traces and logs, retention and compaction run, secrets are correctly kept out of git and out of the image, and the UIs are properly loopback-bound. What is missing is everything above the transport: no application metrics exist at all, Grafana is wired to nothing, and the three log paths both triple the data and disagree on content — while auth tokens and asset credentials are being written into two of them.