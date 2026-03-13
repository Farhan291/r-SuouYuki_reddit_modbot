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

# Expose port (default Flask port)
EXPOSE 8080

# Use gunicorn to serve the Flask app
CMD ["gunicorn", "-b", "0.0.0.0:8080", "-w", "1", "--timeout", "120", "main:app"]