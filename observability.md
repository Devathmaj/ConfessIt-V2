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
  ┌────────────────┐   ┌──────────────────┐  (empty)   ┌────────────────────┐
  │ ~~promtail~~   │   │ Loki 2.9.8       │            │ Tempo 2.5.0        │
  │ ~~:9080~~      │   │ 127.0.0.1:3100   │            │ 127.0.0.1:3200     │
  │ ~~① docker~~   │   │ auth_enabled     │            │ local store /var/  │
  │ ~~  /var/lib/~~│   │ retention 168h   │            │  tempo, block_ret  │
  │ ~~② backend~~  │   │ ruler → :9093 ☠  │            │ 24h                │
  │ ~~  /var/log/~~│   │ compactor ✓      │            │ metrics-generator  │
  │ ~~  confessit/~~│   └──────────────────┘            │ NOT configured ☠   │
  │ REMOVED in     │                                  └────────────────────┘
  │ 4e002d9 (§6.4) │
  └────────────────┘
```

**Log paths, as originally audited (2026-09-16).** All three rows are now history: promtail was deleted, leaving the OTLP path as the only ingestion route (§6.4). Kept because the table is the clearest statement of what H1 cost.

| Path | Mechanism | Loki `job` | Carries uvicorn logs? | Carries app logs? |
|---|---|---|---|---|
| ~~Docker stdout~~ | ~~promtail `docker` glob~~ | ~~`docker`~~ | yes (3011 lines) | yes (3075) |
| ~~`my_app.log` file~~ | ~~promtail `backend-file` glob~~ | ~~`backend-file`~~ | no | yes (7087) |
| **OTLP** | `LoggingHandler` → collector → loki exporter | `confessit-backend` | **yes** (now) | yes |

Verified after the deletion: `{job="docker"}` → 0 records, `{job="backend-file"}` → 0 records. The uvicorn-logs-missing-from-OTLP gap in the last row was also promtail's doing and is closed.

---

## 2. Working and verified

| Component | Status | How verified |
|---|---|---|
| All 10 containers up | ✅ | `docker compose ps` |
| Backend → collector OTLP gRPC | ✅ | Collector self-telemetry: `otelcol_receiver_accepted_spans{transport="grpc"} 2034`, `…log_records 297` |
| Backend → collector → **Tempo** | ✅ | `otelcol_exporter_send_failed_spans{exporter="otlp/tempo"} 0`; fetched trace `cdcc8586…` by ID, 3 spans with full HTTP attributes |
| Backend → collector → **Loki** (OTLP logs) | ✅ | `otelcol_exporter_send_failed_log_records{exporter="loki"} 0`; LogQL `{job="confessit-backend"}` returns records |
| ~~**promtail → Loki** (file + docker)~~ | ❌ **removed** | Promtail was deleted in commit `4e002d9` (2026-09-16). Verified: no promtail container, no config file, no `promtail_positions` volume, no `/var/lib/docker` or `./backend:/var/log` mount. Resolves H1, H2, H3, H6, M3 |
| Log ingestion is single-path | ✅ | `{job="docker"}` → 0 records, `{job="backend-file"}` → 0 records, `{job="confessit-backend"}` → 71 records (last 30 min). Loki's only `job` value is `confessit-backend` |
| Loki stream labels are bounded | ✅ | Label keys are exactly `["exporter","job","level"]`; 3 streams, one per level. No `filename`, no `instance`, no container ID (§6.1) |
| Loki auth (`X-Scope-OrgID`) | ✅ | `tenant_id: confessit` set; `auth_enabled: true`; queries succeed with header |
| **Trace/log correlation exists** | ⚠️ partial | OTLP log body carries `"traceid":"…","spanid":"…"`; that ID resolves in Tempo. But **not** as Loki labels |
| ~~Docker log mount on Windows~~ | ❌ **removed** | Promtail's `/var/lib/docker/containers` mount is gone; this row is why H2 no longer applies (§6.4) |
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
>
> **Datasources resolved; dashboards still open.** `observability/grafana/provisioning/datasources/datasources.yml` now provisions all three with `overwrite: true`, mounted at `docker-compose.yml:225`. Verified live just now: `GET /api/datasources` returns 3 — Prometheus (`confessit-prometheus`, default), Loki (`confessit-loki`, with the `X-Scope-OrgID` header and a `traceid` derived field), Tempo (`confessit-tempo`, with `tracesToLogsV2` back to Loki). The stack is readable through Grafana and the log→trace→log correlation works in both directions.
>
> **The dashboard half is untouched.** `GET /api/search?type=dash-db` returns `[]`, and no `provisioning/dashboards/` directory exists. A fresh `docker compose up` therefore yields a connected but blank Grafana — someone still has to build the first panel by hand (P3#16).
>
> Note the two ports, since they differ and it is easy to query the wrong one: Grafana publishes **3050** on the host, not 3000.

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
>
> **RESOLVED.** Promtail was deleted outright in commit `4e002d9` (2026-09-16), which removed the config, the compose service, the `promtail_positions` volume and both bind mounts. Two of the three paths were promtail's, so ingestion is now single-path. Verified: `{job="docker"}` → 0 records, `{job="backend-file"}` → 0 records, `{job="confessit-backend"}` → 71 records in the last 30 min. Loki's only `job` value is `confessit-backend`.
>
> Note the distinction, since it is easy to over-claim here: the OTLP path can still hold two records with an identical body, because the application genuinely emits the same message more than once (8× `Initializing PostgreSQL storage...` across this session's container recreations). Those are distinct events with distinct timestamps, not duplicate ingestion. The claim is **one ingestion path**, not one record per line.
>
> The file handler in `logger.py` is still attached and still writes `/app/my_app.log` (48 KB and growing). It is now simply not shipped, which is also why M2 and M3 resolved.

**H2 — promtail ships the entire Docker host's logs, including four unrelated projects' containers.** `observability/promtail-config.yml:19` globs `/var/lib/docker/containers/*/*-json.log` with no filtering and no `docker_sd_configs`. Verified: 14 container IDs are ingested, of which 4 are not this project — `timetable-frontend`, `timetable-backend`, `timetable-postgres`, `voucherbot-postgres` (matched against `docker ps`).
> Impact: another application's logs — potentially its users' data and its own connection strings — are copied into ConfessIt's Loki. Nothing in the label set distinguishes them (see H6).
>
> **RESOLVED** by the same deletion. No promtail container exists, `/var/lib/docker/containers` is mounted nowhere in `docker-compose.yml`, and Loki contains no foreign containers' lines. This is why no `docker_sd_configs` allowlist was added instead: there is no discovery mechanism left to constrain. See §6.4 if container logs are ever wanted back — the allowlist is not optional then.

**H3 — promtail mounts the entire `backend/` directory, including the secrets file.** `docker-compose.yml:167` mounts `./backend:/var/log/confessit:ro`, and `backend/.env` contains `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_JWT_SECRET`, `CLOUDINARY_API_SECRET`, `CLOUDINARY_URL` (key names verified; values not printed). Verified present inside the container at `/var/log/confessit/.env`.
> Impact: **not currently exfiltrated** — the glob is `*.log*` (`promtail-config.yml:26`), which does not match `.env`. But the entire secret-bearing tree is handed to a container whose only job is to read files and ship them. A one-character glob change (`*`) exfiltrates every credential.
>
> **RESOLVED.** The `./backend:/var/log/confessit:ro` mount is gone. Verified against every bind mount in the file: the only remaining `./backend` mount is `./backend:/app` on the backend service itself (line 39), which is the application reading its own code and its own environment — the normal case, not a finding. No observability container has any access to `backend/.env`, and the one-character-glob failure mode described above no longer exists because there is no glob.

**H4 — Tempo's metrics-generator is disabled, so no service graphs and no RED metrics from traces.** Verified: `docker logs tempo` → `level=warn … msg="metrics-generator is not configured." err="no metrics_generator.storage.path configured"`. `observability/tempo.yml` has no `metrics_generator` block, and no remote-write to Prometheus. `/api/metrics/query_range` and `/api/metrics/summary` are served but have no data source.
> Impact: trace-derived RED metrics are unavailable. Combined with C2, there is no metrics path at all.

**H5 — Traces contain no child spans. No DB, Redis, or outbound-HTTP instrumentation.** Verified: every trace inspected (`81bb49c4…`, `cdcc8586…`) has exactly 3 spans, all from `FastAPIInstrumentor`/ASGI (`GET /` SERVER + two `http send` INTERNAL). `backend/requirements.txt` has no `opentelemetry-instrumentation-sqlalchemy`, `-psycopg`, `-redis`, or `-requests/-httpx`. The app uses raw SQLAlchemy/psycopg (`app/postgres.py`), Redis pub/sub (`app/services/redis_service.py`), and Cloudinary over HTTP.
> Impact: traces answer "how long did the request take" and nothing about "what did it do". A slow query is indistinguishable from a slow handler.

**H6 — The `docker` job has no usable labels.** Verified: Loki's entire label set is `["exporter","filename","job","level"]`. There is no `container`, `service`, or `stream` label. Streams are keyed by `filename` = the ephemeral 64-hex container ID, so **every `docker compose up` that recreates a container opens a brand-new stream set and orphans the old one**. Verified from `docker logs promtail`: the `docker` job's lines are the raw `{"log":…,"stream":…,"time":…}` JSON envelope with `stream` (stdout/stderr) buried in the body.
> Impact: H2's foreign containers and this project's own containers are indistinguishable in the UI except by cross-referencing a container-ID hash. This also creates a **feedback loop**: verified that Loki's own query-access logs (which embed the full LogQL query text) are ingested into Loki, so every query writes new logs.
>
> **RESOLVED, twice over.** The `docker` job no longer exists — promtail's deletion removed the `filename` label along with the pipeline, so the per-container-ID stream churn and the query feedback loop are both gone. Loki's label key set is now exactly `["exporter","job","level"]`, and the only streams are `level=INFO`, `level=WARN`, `level=ERROR`.
>
> Separately, the **OTLP path had the same defect in a different form**: `service.instance.id` is a per-process UUID that the Loki exporter promotes to an `instance` label, so it was churning one stream per recreation too. Fixed in §6.1 by deleting that attribute on the logs pipeline. Measured before: 8 distinct `instance` values in 15 minutes. This is the churn the current request was aimed at, and it is why §6.1 exists.
>
> The `container` / `service` dimension this finding wanted is now supplied by `job`, which comes from `service.name` — the Compose service name, which is stable. It is `job` rather than `service` because that is the name the OTLP exporter derives it as; adding a second label with the same value would be a duplicate, not an extra dimension.

**H7 — Zero alerting, and dead alerting config.** Verified: `GET /api/v1/rules` → `{"groups":[]}`; `GET /api/v1/alertmanagers` → `{"activeAlertmanagers":[]}`; no Alertmanager container exists. `observability/loki-config.yml:42-43` sets `ruler: alertmanager_url: http://localhost:9093` with no `rule_files` and an empty `/loki/rules` — a ruler that cannot alert and points at a port nothing listens on. `observability/prometheus.yml` has no `rule_files` and no alerting block.

> **Partly resolved; see §6.2.** `rule_files` and the Prometheus alerting path are now real — 4 rules load and evaluate, including a `ConfessItBackendDown` rule that catches the backend crash this finding did not anticipate. The **Alertmanager half is untouched**: no rule notifies anyone yet, and the dead Loki `ruler` block still points at `localhost:9093`.

**H8 — PostgreSQL is published to the LAN.** `docker-compose.yml:64-65` uses `ports: - "5432:5432"` (0.0.0.0) while every observability UI is deliberately loopback-bound. Verified: `Test-NetConnection 172.17.80.1:5432` → **True** (contrast 3050/3100/3200/9090 → all False).
> Impact: inconsistent with the file's own stated security posture; the DB is the highest-value target in the stack.

### MEDIUM

**M1 — Trace IDs are not queryable as Loki labels.** Verified: the OTLP stream's labels are exactly `{"exporter":"OTLP","job":"confessit-backend","level":"INFO"}`. `traceid`/`spanid` exist only inside the JSON body. Correlation therefore requires `| json | traceid="…"` rather than `{trace_id="…"}`, and the `docker`/`backend-file` copies carry **no trace ID at all** (plain text lines). So the two paths you'd actually correlate from disagree.
> The finding still stands on trace IDs. The label-set measurement was taken from a stream that did not happen to carry `instance`; the OTLP path was in fact also keying streams on `service.instance.id` and churning one stream per process. Fixed in §6.1 — label keys are now exactly `{exporter, job, level}`.

**M2 — The three log paths carry different content.** Verified over 24h: `"Uvicorn running"` → `docker` 3011, `backend-file` **0**, `confessit-backend` **0**. Uvicorn's lifecycle/access logs exist *only* on the stdout path. Separately, `backend/app/logger.py:71` sets `LoggingHandler(level=logging.INFO)` while `logger.py:76` sets the logger to `DEBUG`, so any future DEBUG record would reach the file and console but be **silently dropped** from OTLP.
> **Largely resolved.** Two of the three disagreeing paths were promtail's and no longer exist (H1). Uvicorn's lifecycle lines now reach Loki over OTLP, so the "present on one path only" case is gone.
> **The DEBUG-drop half stands.** `LoggingHandler(level=logging.INFO)` is still the OTLP handler's level against a `DEBUG` logger, so a future DEBUG record would still be silently dropped. Still 0 DEBUG records today. Fixing it means aligning the handler level, not adding a log path.

**M3 — promtail re-ingests rotated log archives.** `observability/promtail-config.yml:26` globs `*.log*`, matching `my_app.log.2025-08-22` … `my_app.log.2026-09-26` (11 rotated files, verified in Loki's `filename` values). Verified churn in `docker logs promtail`: repeated `skipping update of position for a file which does not currently exist` / `stopped tailing file` for `my_app.log.2025-08-23`, `2025-10-25`, `2025-10-30`. A year of history is re-ingested whenever positions or the Loki volume are lost, restarting the 7-day retention clock with stale data.
> **RESOLVED** by the promtail deletion — there is no glob and no position file, so nothing re-ingests archives. The rotated `my_app.log.*` files are still on disk in `backend/` and are now simply never read, which also makes them dead weight rather than a live risk. Worth knowing they exist before any future log shipper is added.

**M4 — The collector's own telemetry is never scraped.** The collector serves self-metrics on `:8888` (verified: `otelcol_receiver_accepted_spans 2034`, `otelcol_exporter_send_failed_* 0`) and the app-facing exporter on `:8889`. `observability/prometheus.yml:10-12` scrapes only `8889`, which is empty. The single most useful health signal in the stack — proof that pipelines are moving data — is invisible to Prometheus.

**M5 — Stale OTel semantic conventions.** Verified span attributes: `http.method`, `http.status_code`, `http.flavor`, `http.target`, `net.peer.ip` — the pre-1.20 convention, from `opentelemetry-instrumentation-fastapi==0.46b0` (`backend/requirements.txt:22`). Current convention is `http.request.method` / `http.response.status_code` / `network.protocol.version` / `client.address`.
> Impact: modern Tempo span metrics, service graphs, and Grafana's default Tempo panels key off the new names and will not match.

> **RESOLVED.** The OTel stack is now `1.45.0` / `0.66b0` with `opentelemetry-semantic-conventions==0.66b0`, and `OTEL_SEMCONV_STABILITY_OPT_IN=http` is set on the backend in `docker-compose.yml`. Verified on live spans: `http.request.method`, `http.response.status_code`, `network.protocol.version`, `url.path`, `url.scheme`, `client.address` and `server.address` are all present, and the pre-1.20 names are **absent** — this is a clean break, not dual emission. The version bump alone did **not** change any attribute; 0.66b0 still defaults to the old conventions, the opt-in is what turns the new ones on, and the value of that opt-in is what decides whether the old ones are retired alongside them. `url.full` is absent on server spans because upstream only sets it on client spans, and `error.type` appears only on error responses.
>
> Migration consequences, all of which were carried rather than worked around: `http.server.duration` (ms) is replaced by `http.server.request.duration` (s), so the PromQL in `observability/alerting-rules.yml` and `README.md` was rewritten and the H7 latency threshold moved from `1000` to `1` to match the new unit and the new bucket boundaries; `http_status_code` → `http_response_status_code`; `http_target` → `http_route`. `BOUNDED_HTTP_METRIC_ATTRIBUTES` in `backend/app/main.py` now allowlists only current names. The one deliberate exception is `app/telemetry.py`, which still redacts the pre-1.20 `http.url` and `http.target`: that is defence in depth against a span from another source rather than compatibility handling for this application's own emission, and it keeps C4 closed for any instrumentor that has not opted in.
>
> Database and Redis spans deliberately remain on the pre-1.20 `db.*` names (`database` was not enabled), and the H4 spanmetrics labels are unchanged as a result.

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

> **Qualification, added 2026-09-27.** This list records what the audit observed, and it is now history for the findings marked resolved above. H1, H2, H3, H6, M2, M3 and M5 were each verified *as defects* against a stack that no longer exists in that state: the promtail path they all depended on was deleted in `4e002d9`, and the semconv stack was modernised afterwards. Their resolutions were re-verified against the running system, but the original observations should not be cited as current behaviour. In particular the §2 row claiming 8703 `docker` and 7672 `backend-file` lines was true when written and is now false; it is struck through rather than deleted so the change is visible.

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
6. ~~Narrow the promtail backend mount~~ — **done, obsoleted.** Promtail deleted in `4e002d9`; H3 resolved (§6.4).
7. ~~Change the `docker` scrape to a relabeled, allow-listed form~~ — **done, obsoleted.** The `docker` job no longer exists; H2 and H6 resolved (§6.4).

**P2 — correctness and cost**
8. ~~Pick one ingestion path~~ — **done.** Only the OTLP path remains; the file handler still writes `my_app.log` but nothing ships it. Fixes H1, and M2/M3 by the same deletion (§6.4).
9. Promote `traceid` to a Loki label in the collector's `loki` exporter, and add `service_name` as a label. Fixes M1. **Do not** promote `service.instance.id` to a label to achieve this — see §6.1.
10. Configure Tempo `metrics_generator` with a `remote_write` to Prometheus. Fixes H4 and gives RED from traces even before P0#2 lands.
11. Change `docker-compose.yml:64-65` to `127.0.0.1:5432:5432`, matching the file's own stated posture. Fixes H8.
12. Install `LoggingInstrumentor` (already a dependency) or add a `LoggingHandler` filter, and pin the OTLP handler level to match the logger level. Fixes the remaining half of M2 and retires L2.

**P3 — operability**
13. Add `healthcheck:` + `depends_on: condition: service_healthy` for loki/tempo/prometheus. Fixes M6.
14. Either add Prometheus `rule_files` plus an Alertmanager, or delete the `ruler` block and the `alertmanager_url` from `loki-config.yml`. Right now both alerting paths are decorative. Fixes H7. **The Alertmanager is still the outstanding half** (§6.2) — `rule_files` are in place and firing, but into nothing.
15. Add `opentelemetry-instrumentation-redis` and an `httpx`/requests instrumentor for Cloudinary calls; refresh `opentelemetry-instrumentation-fastapi` to a current version for modern semconv. Fixes H5, M5.
16. Provision a minimal dashboard set (Golden Signals + a trace-to-log panel) once datasources exist.
17. Set `usage_report.reporting_enabled: false` in `tempo.yml`; drop `--reload` from the compose command; remove the inert Redis maxmemory policy and the `GF_AUTH_ANONYMOUS_ORG_ROLE` line. Fixes L1, L3, L4, L5.

---

## 6. Follow-up — 2026-09-27 (gaps found after the modern-conventions migration)

Three gaps surfaced while verifying the modern semconv work (§6.1–6.3), none of them covered by the numbered findings above. A fourth (§6.4) records that the log-pipeline churn this document was asked about had already been fixed by an earlier commit.

### 6.1 The OTLP log pipeline was churning one Loki stream per process

**Symptom.** `service.instance.id` was reaching Loki as a stream label, so every `docker compose up` that recreated the backend opened a new stream set and orphaned the old one — the same failure mode as H6, on a different pipeline. Measured: 8 distinct `instance` values (per-process UUIDs) in the 15 minutes before the fix, one per restart.

The `docker` job in H6 is a *different* path (promtail, keyed by `filename` = container ID) and was untouched by this fix; it was resolved separately by promtail's deletion, recorded in §6.4. Note that M1's label-set measurement was taken from a stream that did not happen to carry the attribute, which is why the finding understated it.

**Fix.** A `resource/drop_instance_id` processor in `observability/otel-collector-config.yml` deletes `service.instance.id` on the `logs` pipeline only. Traces keep the real per-process resource ID, which is what you actually want for distinguishing replicas in a trace view.

**Verified.** Loki volume cleared, collector and backend recreated, fresh traffic generated. Label keys are now exactly `{exporter, job, level}` and `instance` is absent — **1 stream**, against 8+ before.

### 6.2 Nothing could report the backend being down

**Gap.** H7's fix added RED rules and a pipeline rule, but a pushed pipeline structurally cannot report on its own producer: `ConfessItBackendMetricsPipelineDown` watches `up{job="otel-collector"}`, and that target stays `up` whether or not anything is feeding it. If the backend crashed, every RED query would flatline at zero and no rule would fire — a total outage that is indistinguishable from "no traffic".

**Fix.** A `GET /metrics` endpoint on the backend exposing only `confessit_backend_up` and `confessit_backend_start_time_seconds`, scraped by Prometheus as job `confessit-backend`, plus a `ConfessItBackendDown` rule on `up{job="confessit-backend"} == 0` for 2m.

The endpoint deliberately does **not** re-export the OTel instruments. They already arrive over OTLP, and a second copy under a different `job` label would double-count every RED query that does not pin `exported_job` — a silent, hard-to-spot regression. The series are unlabelled; a label on a liveness gauge is just a way to mint a stream per value.

**Verified end to end, not just by config.** Backend stopped, collector deliberately left running: `up` went to `0`, `ConfessItBackendDown` reached `state=firing` ("Backend is not responding to scrapes"), and `ConfessItBackendMetricsPipelineDown` correctly stayed `inactive` — the two failure modes are distinguishable. After restart, `up` returned to `1` and the rule cleared.

### 6.3 `app.telemetry` imported from the private semconv tree

**Gap.** It imported `HTTP_URL` / `HTTP_TARGET` from `opentelemetry.semconv._incubating.attributes.http_attributes`. The module is imported by `app.main` at startup, so a dependency on an explicitly-unstable, underscore-prefixed path would break the boot of the whole service on a package layout change — for two string constants the spec fixes.

**Fix.** `_PRE_1_20_URL = "http.url"` / `_PRE_1_20_TARGET = "http.target"` literals, still in `_URL_ATTRIBUTES` so C4 stays closed. `test_pre_1_20_names_match_the_sdk_constants` cross-checks them against the SDK, so the literals cannot silently drift.

### Still open

`ConfessItBackendDown` **fires into Prometheus and nothing else.** There is still no Alertmanager (`GET /api/v1/alertmanagers` → `{"activeAlertmanagers":[]}`), so no rule in this stack notifies anyone — the firing state is only visible in the Prometheus UI and the `/api/v1/rules` API. The other half of H7 remains: add an Alertmanager and an `alerting:` block, or these are dashboard widgets rather than alerts.

Verification for all three: 19/19 live checks, 314 passed / 1 skipped in the backend suite, and 8/8 `promtool test rules` cases.

### 6.4 Log-pipeline churn: already fixed, by deletion rather than relabeling

The request that prompted this section was to stop ephemeral container IDs becoming Loki stream labels and to key streams on stable Compose service/container labels instead. **No code change was needed — the pipeline that had the defect was removed on 2026-09-16 in commit `4e002d9`**, which is a strictly better outcome than relabeling it. Recorded here so the reasoning survives, and so nobody re-adds promtail casually.

**What existed when the audit was written.** `observability/promtail-config.yml` globs `/var/lib/docker/containers/*/*-json.log` and keys streams on `filename`, the 64-hex container ID. H2, H3 and H6 are all consequences of that one line.

**What exists now.** Loki's label key set is `["exporter","job","level"]` across its entire volume. There is no `filename` label because there is no promtail. The three findings resolved:

| Finding | Root cause | Resolution |
|---|---|---|
| H2 — foreign containers ingested | unfiltered `/var/lib/docker/containers` glob | No promtail, no glob. Loki holds no foreign containers' lines |
| H3 — `backend/.env` reachable by a log shipper | `./backend:/var/log/confessit:ro` | Mount gone. The only `./backend` mount left is `./backend:/app` on the backend itself |
| H6 — one stream set per container ID | `filename` = container ID | Label and pipeline both gone; `job` (= Compose service name) supplies the service dimension |
| H1, M3 — triple ingestion, re-ingested archives | promtail shipping the same logs twice more | Single OTLP path, 0 records on the other two |

**Why not re-add promtail with an allowlist.** It is the obvious next question, so the answer is recorded. `docker_sd_configs` with a `label` allowlist does fix H2's mechanism, and it is how this should be done if container logs are ever wanted. But H3's exposure returns with the mount, and the OTLP path already delivers the backend's logs with no filesystem access at all — so promtail would add a second path for the one service that does not need it, in exchange for the logs of `loki`, `tempo`, `prometheus`, `grafana`, `otel-collector` and `frontend`, which the stack currently does not collect at all.

**If that trade is ever made**, three conditions are not optional: a `docker_sd_configs` allowlist restricted to `com.docker.compose.project=confessit_v2`; no mount of any directory containing secrets; and `relabel_configs` replacing `filename` with the Compose service label, so streams key on `service` and never on a container ID. The third is the actual ask in this section — worth doing properly, since the label churn is the part that is expensive and slow to notice.

**Also resolved incidentally.** The audit's feedback loop — Loki ingesting its own query-access logs, so every query wrote new logs — cannot happen while nothing ships Loki's logs. The file handler in `backend/app/logger.py` still writes `/app/my_app.log` and the ~11 rotated `my_app.log.*` archives are still on disk; they are now unread rather than a live risk, and would become a retention-clock problem again the moment any glob is reintroduced.

---

**Bottom line:** the transport layer is genuinely well built and healthy. Ingestion works end to end for traces and logs, retention and compaction run, secrets are correctly kept out of git and out of the image, and the UIs are properly loopback-bound. The log pipeline in particular is now in good shape: one ingestion path, a bounded label set, and no container-ID stream churn (§6.4). Grafana is connected to all three datasources, so the read surface exists.

What is still missing is everything *above* the transport. No dashboard is provisioned, so nobody has a Golden Signals view without building one by hand. Alerts evaluate but are delivered nowhere — there is no Alertmanager (§6.2). Loki's own `ruler` still points at a port nothing listens on. And the stack is blind to the other eight of its nine services: only the backend's logs, traces and metrics are collected at all.