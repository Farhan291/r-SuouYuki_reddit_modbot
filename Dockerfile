# Use official Python slim image
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Create working directory
WORKDIR /app

# Install python dependencies
COPY requirements.txt ./
RUN pip install --no-cache-dir uv && \
    uv pip install --system -r requirements.txt

# Copy application code
COPY . .

# Expose port for health check endpoint
EXPOSE 8080

# Run the bot directly as a single process (NOT gunicorn)
CMD ["python", "main.py"]
