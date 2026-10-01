# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# cron powers the game-scheduler sidecar (same image, overridden command): the
# 00:01 UTC game reset trigger. Installed here rather than in a second
# Dockerfile so the sidecar shares the exact interpreter and dependencies the
# app ships; the API process itself never starts cron.
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

# Ensure pip and wheel are up-to-date. setuptools itself comes from
# requirements.txt (pinned and audited there): the old `setuptools<81` step for
# pkg_resources is gone because nothing imports it, and keeping it only meant
# installing one setuptools to have requirements.txt immediately replace it
# (DEP-001).
RUN pip install --no-cache-dir --upgrade pip wheel
RUN pip install --no-cache-dir -r requirements.txt

# watchfiles comes in transitively through uvicorn[standard], and its reloader
# crashes with `WatchfilesRustInternalError: Input/output error (os error 5)` on
# this development host's bind-mounted filesystem -- WATCHFILES_FORCE_POLLING does
# not help because that only switches to watchfiles' own Rust-backed poller, which
# hits the same error. With watchfiles absent, uvicorn falls back to its
# pure-Python StatReload (mtime polling), which watches bind mounts reliably. Nothing
# else in this app imports watchfiles, so uninstalling it is safe here.
RUN pip uninstall -y watchfiles

# Unprivileged account for the game-scheduler sidecar's cron job and catch-up
# pass (see scripts/game-scheduler-entrypoint.sh): the reset needs the database
# and the app tree, not root. The API and the entrypoint itself still start as
# root -- cron must, to read cron.d and change user -- only the tick runs here.
RUN useradd -r -m appuser

# Expose the port the app runs on
EXPOSE 8001

# Copy the rest of the application code
COPY . .

# The sidecar reads the tree as appuser, so it must own it in the image. (Under
# the dev bind mount the host owns the files instead; world-readable host
# permissions are what the sidecar relies on there.)
RUN chown -R appuser:appuser /app

# Run the FastAPI app with auto-reload enabled.
# --limit-concurrency caps how many request bodies can be in flight at once and
# --h11-max-incomplete-event-size bounds the bytes buffered for an unfinished request.
# Both are pinned explicitly rather than left to uvicorn's defaults, so a dependency
# bump cannot silently loosen them. Per-body size is enforced in app.main.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload", \
     "--limit-concurrency", "100", "--h11-max-incomplete-event-size", "16384"]
