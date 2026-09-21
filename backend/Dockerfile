# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Ensure pip and wheel are up-to-date, then install a setuptools version that still provides pkg_resources
RUN pip install --no-cache-dir --upgrade pip wheel "setuptools<81"
RUN pip install --no-cache-dir -r requirements.txt

# watchfiles comes in transitively through uvicorn[standard], and its reloader
# crashes with `WatchfilesRustInternalError: Input/output error (os error 5)` on
# this development host's bind-mounted filesystem -- WATCHFILES_FORCE_POLLING does
# not help because that only switches to watchfiles' own Rust-backed poller, which
# hits the same error. With watchfiles absent, uvicorn 0.23.2 falls back to its
# pure-Python StatReload (mtime polling), which watches bind mounts reliably. Nothing
# else in this app imports watchfiles, so uninstalling it is safe here.
RUN pip uninstall -y watchfiles

# Expose the port the app runs on
EXPOSE 8001

# Copy the rest of the application code
COPY . .

# Run the FastAPI app with auto-reload enabled.
# --limit-concurrency caps how many request bodies can be in flight at once and
# --h11-max-incomplete-event-size bounds the bytes buffered for an unfinished request.
# Both are pinned explicitly rather than left to uvicorn's defaults, so a dependency
# bump cannot silently loosen them. Per-body size is enforced in app.main.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload", \
     "--limit-concurrency", "100", "--h11-max-incomplete-event-size", "16384"]
