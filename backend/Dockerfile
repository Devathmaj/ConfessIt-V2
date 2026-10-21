# Production image for the ConfessIt API and the game-scheduler cron sidecar.
# Development uses Dockerfile.dev (bind mount + uvicorn --reload).
# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Install cron for game-scheduler resets sidecar.
RUN apt-get update && apt-get install -y --no-install-recommends cron \
    && rm -rf /var/lib/apt/lists/*

# Upgrade build tools before installing pinned dependencies.
RUN pip install --no-cache-dir --upgrade pip wheel
RUN pip install --no-cache-dir -r requirements.txt

# No reloader runs here, so the reload-only watcher is not shipped.
RUN pip uninstall -y watchfiles

# Unprivileged user for cron execution.
RUN useradd -r -m appuser

# Expose the port the app runs on
EXPOSE 8000

# Copy the rest of the application code
COPY . .

# Grant ownership of app tree to appuser.
RUN chown -R appuser:appuser /app

# Run FastAPI with bounded concurrency, no reload.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--limit-concurrency", "100", "--h11-max-incomplete-event-size", "16384"]
