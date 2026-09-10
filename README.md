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

The names below are the ones this stack actually produces, which are **not** the `http_server_duration_seconds` spelling in current OpenTelemetry documentation. The pinned instrumentation (`0.46b0`) names the histogram `http.server.duration` in milliseconds, and the collector's Prometheus exporter appends the unit:

| Prometheus name | Meaning |
| --- | --- |
| `http_server_duration_milliseconds_bucket` / `_sum` / `_count` | Request latency histogram, and the count behind request rate |
| `http_server_active_requests` | Requests currently in flight |
| `http_server_response_size_bytes_*` | Response size (request size appears only for requests with a `Content-Length`) |

The four things the stack is read for come from these:

```promql
sum by (http_target, http_status_code) (rate(http_server_duration_milliseconds_count[5m]))                       # rate + count
sum(rate(http_server_duration_milliseconds_count{http_status_code=~"5.."}[5m]))                                  # error rate
histogram_quantile(0.95, sum by (le) (rate(http_server_duration_milliseconds_bucket[5m])))                        # p95 latency
```

Dimensions are `http_method`, `http_status_code`, `http_target`, `http_scheme` and `http_flavor`. `http_target` is the **route template** (`/confessions`, `/confessions/{confession_id}`), not the concrete path, so one series covers every confession id. `http.server_name` — the raw `Host` header — is deliberately excluded by a View in `backend/app/main.py`, because the backend listens on `0.0.0.0:8001` and an unbounded dimension there would let any client mint time series by varying that header. The query string never appears in metrics.
