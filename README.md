# ConfessIt

ConfessIt is a full-stack web application that provides a safe and anonymous space for users to share confessions, connect with others, and engage in fun activities. It features anonymous confessions, matchmaking, love notes, and mini-games.

## Features

- **Anonymous Confessions:** Share your thoughts and feelings without revealing your identity.
- **Matchmaking:** Find and connect with other users on the platform.
- **Love Notes:** Send and receive anonymous love notes.
- **Mini-Games:** Play fun games to break the ice.
- **User Profiles:** Customize your profile with a bio, interests, and more.
- **Authentication:** Secure authentication with magic links.

## Tech Stack

- **Frontend:** React, TypeScript, Tailwind CSS, Shadcn UI
- **Backend:** FastAPI, Python
- **Database:** PostgreSQL 16 with SQLAlchemy ORM and psycopg
- **Containerization:** Docker, Docker Compose

## Prerequisites

Before you begin, ensure you have the following installed:

- [Docker](https://docs.docker.com/get-docker/)
- [Docker Compose](https://docs.docker.com/compose/install/)

## Getting Started

1. **Clone the repository:**

   ```bash
   git clone https://github.com/your-username/confessit.git
   cd confessit
   ```

2. **Create the local credential files:**

   Both `.env` files are gitignored, so a fresh clone has neither and the stack will refuse to start without them. Copy the values you need into:

   - `.env` (repo root): `POSTGRES_PASSWORD`, `REDIS_PASSWORD` and `GRAFANA_ADMIN_PASSWORD`. Docker Compose reads this file to resolve `${VAR}` references in `docker-compose.yml`, injecting them as environment variables when the stack starts. A missing value fails the start-up immediately instead of falling back to a password committed to git.
   - `backend/.env`: application secrets, plus the `REDIS_URL` template. The backend reads this file from `/app` at run time, and python-dotenv substitutes `${REDIS_PASSWORD}` there from its container environment, so the root `.env` stays the only file the Redis password is written in.

3. **Build and run the application with Docker Compose:**

   ```bash
   docker-compose up --build
   ```

   This command will build the Docker images for the frontend and backend services and start the containers.

4. **Access the application:**

   - **Frontend:** Open your browser and navigate to `http://localhost:5173`
   - **Backend API:** The API will be running at `http://localhost:8001`. You can access the API documentation at `http://localhost:8001/docs`.
   - **Grafana:** `http://localhost:3050`, logging in with `GRAFANA_ADMIN_USER` from the root `.env`.

5. **Grafana needs no manual configuration.** Prometheus, Loki and Tempo are provisioned from `observability/grafana/provisioning/datasources/datasources.yml` on every start, so a fresh `docker compose up` yields a Grafana that is already connected. The Loki datasource carries the `X-Scope-OrgID: confessit` header that Loki requires, matching what the otel-collector sends, so Explore returns the same tenant the stack writes to.

   The observability UIs are bound to `127.0.0.1`, so they are reachable from the host but not from the network.

## Project Structure

```
backend/
  app/
    routers/
    services/
    db_models.py
    postgres.py
  Dockerfile
  requirements.txt
frontend/
  src/
  Dockerfile
  package.json
docker-compose.yml
postgres-init.sql
README.md
```

## API Endpoints

The backend API provides the following endpoints:

- `/auth/login/magic`: Generate a magic link for authentication.
- `/auth/login/magic/verify`: Verify a magic link token.
- `/auth/me`: Get the current user's details.
- `/profile/update`: Update the current user's profile.
- `/profile/upload-picture`: Upload a profile picture.
- `/matchmaking/potential-matches`: Get a list of potential matches.
- `/matchmaking/find`: Find a random match.
- `/confessions`: Create and get confessions.
- `/confessions/{confession_id}/react`: React to a confession.
- `/confessions/{confession_id}/comment`: Comment on a confession.
- `/comments/{comment_id}/like`: Like a comment.
- `/comments/{comment_id}/dislike`: Dislike a comment.

For more details, see the API documentation at `http://localhost:8001/docs`.

## Environment Variables

- `POSTGRES_PASSWORD`: PostgreSQL password for the Compose database. Set it in the root `.env`; Compose requires it and interpolates it into `DATABASE_URL`. The local value is intended for development, so use a strong one for deployments.
- `REDIS_PASSWORD`: Redis password for the Compose cache, defined once in the root `.env`. Compose requires it, passes it to the server as `--requirepass`, and injects it into the backend so the `REDIS_URL` template can resolve it. Redis is not published to the host, so reach it with `docker compose exec redis redis-cli` rather than a local client.
- `GRAFANA_ADMIN_USER` / `GRAFANA_ADMIN_PASSWORD`: Grafana credentials, set in the root `.env`. Grafana only applies the password when it initialises an empty database, so after changing it on an existing `grafana_data` volume run `docker compose exec grafana grafana cli admin reset-admin-password <value from .env>`.
- `CLOUDINARY_SECURE_URLS`: Set to `false` only if Cloudinary assets must be delivered over plain HTTP. Defaults to `true`, so asset URLs are HTTPS.
- `OTEL_EXPORTER_OTLP_INSECURE`: Set to `false` when the OTLP collector is not on the local Compose network, together with `OTEL_EXPORTER_OTLP_CERTIFICATE` pointing at the CA bundle. The backend refuses to export telemetry over TLS without a certificate to verify the collector with. Defaults to `true` for the local collector, which has no certificates.
- `DATABASE_URL`: PostgreSQL connection string used by the backend. Compose configures it automatically.

## Application metrics

The backend exports HTTP server metrics over the same OTLP pipeline as the traces and logs: FastAPI → OTLP gRPC → `otel-collector` → the collector's Prometheus exporter on `:8889` → Prometheus, which `prometheus.yml` already scrapes. No separate metrics architecture and no Prometheus client library are involved.

The names below are the ones this stack actually produces. The backend sets `OTEL_SEMCONV_STABILITY_OPT_IN=http`, so only the current HTTP semantic conventions are reported — the pre-1.20 names are not emitted at all — and the collector's Prometheus exporter appends each instrument's unit:

| Prometheus name | OTel instrument | Meaning |
| --- | --- | --- |
| `http_server_request_duration_seconds_bucket` / `_sum` / `_count` | `http.server.request.duration` | Request latency histogram in **seconds**, and the count behind request rate |
| `http_server_active_requests` | `http.server.active_requests` | Requests currently in flight |
| `http_server_request_body_size_bytes_*` | `http.server.request.body.size` | Request size (appears only for requests with a `Content-Length`) |
| `http_server_response_body_size_bytes_*` | `http.server.response.body.size` | Response size |

Dimensions are `http_request_method`, `http_response_status_code`, `http_route`, `url_scheme` and `network_protocol_version`. The route dimension is the **route template** (`/confessions`, `/confessions/{confession_id}`), not the concrete path, so one series covers every confession id. The histogram's bucket boundaries are `0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10` seconds, so a `histogram_quantile` reading is only meaningful to within a bucket.

Note the unit when writing a latency threshold: this instrument is in **seconds**, not the milliseconds of the retired `http_server_duration_milliseconds_*` histogram, so a "slow" threshold is `1`, not `1000`.

The four things the stack is read for:

```promql
sum by (http_route, http_response_status_code) (rate(http_server_request_duration_seconds_count[5m]))                # rate + count
sum(rate(http_server_request_duration_seconds_count{http_response_status_code=~"5.."}[5m]))                           # error rate
histogram_quantile(0.95, sum by (le) (rate(http_server_request_duration_seconds_bucket[5m])))                         # p95 latency
```

`http.server_name` — the raw `Host` header — is excluded by a View in `backend/app/main.py`, which allowlists exactly the five dimensions above. The View fails closed: an attribute a future release starts emitting is excluded until someone decides it belongs, and it is what keeps a Host-derived dimension from becoming a cardinality source on a backend that listens on `0.0.0.0:8001`. The query string never appears in metrics.

## Liveness and the scrape endpoints

A pushed pipeline cannot report on its own producer. The metrics above reach Prometheus by way of the collector, so nothing in that path says whether the backend is alive — the collector is perfectly healthy whether or not anything is feeding it. Two scrape endpoints exist to cover the two things that path cannot:

| Endpoint | Job | Series | Carries |
| --- | --- | --- | --- |
| `backend:8001/metrics` | `confessit-backend` | `up`, `confessit_backend_up`, `confessit_backend_start_time_seconds` | Backend process liveness only |
| `otel-collector:8888` | `otel-collector-internal` | `up`, `otelcol_*` | Collector receiver and exporter counters |

`GET /metrics` on the backend serves a two-series Prometheus exposition and **nothing else**. It deliberately does not re-export the OTel instruments: the application metrics already arrive over OTLP, and a second copy under a different `job` label would double-count every RED query above that does not happen to pin `exported_job`. The series are unlabelled by design — a label on a liveness gauge is a way to mint a stream per value. `confessit_backend_start_time_seconds` is what makes a restart visible: a gauge that silently resets to `1` is easy to miss, a start time that jumps is not.

This is what `ConfessItBackendDown` (`up{job="confessit-backend"} == 0`) watches, and it is distinct from `ConfessItBackendMetricsPipelineDown` (`up{job="otel-collector"} == 0`): the first is the application being down, the second is the application running while its metrics are not getting through.
