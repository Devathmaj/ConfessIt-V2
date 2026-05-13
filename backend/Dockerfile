# Use an official Python runtime as a parent image
FROM python:3.12-slim

# Set the working directory in the container
WORKDIR /app

# Copy the requirements file into the container
COPY requirements.txt .

# Ensure pip and wheel are up-to-date, then install a setuptools version that still provides pkg_resources
RUN pip install --no-cache-dir --upgrade pip wheel "setuptools<81"
RUN pip install --no-cache-dir -r requirements.txt

# Expose the port the app runs on
EXPOSE 8001

# Copy the rest of the application code
COPY . .

# Run the FastAPI app with auto-reload enabled
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8001", "--reload"]
