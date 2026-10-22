# Production API and scheduler share installed Python dependencies. The scheduler
# target alone adds cron; the API image stays smaller and has fewer packages.
FROM python:3.12.15-slim-bookworm AS runtime-base

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Upgrade build tools before installing pinned dependencies.
RUN pip install --no-cache-dir --upgrade pip wheel \
    && pip install --no-cache-dir -r requirements.txt

# No reloader runs here, so the reload-only watcher is not shipped.
RUN pip uninstall -y watchfiles

# Unprivileged runtime account.
RUN useradd -r -m appuser

# Expose the port the app runs on
EXPOSE 8000

# Copy only runtime code, owned by the unprivileged runtime account.
COPY --chown=appuser:appuser . .

USER appuser

# Run FastAPI with bounded concurrency, no reload.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--limit-concurrency", "100", "--h11-max-incomplete-event-size", "16384"]

# The scheduler requires cron to install and run its scheduled job. Compose
# selects this target only for game-scheduler.
FROM runtime-base AS scheduler
USER root
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*
