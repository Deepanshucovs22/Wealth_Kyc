# syntax=docker/dockerfile:1
# Multi-stage build for optimized production image
FROM python:3.12-slim AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv venv
ENV PATH="/app/venv/bin:$PATH"

COPY requirements.txt .

# Vendored private wheels (e.g. cams-otel-lib) — committed under docker/vendor/ so the
# build needs no GCP Artifact Registry credentials/service-account secret. requirements.txt
# still declares these packages normally (e.g. cams-otel-lib>=0.2.0); --find-links tells
# pip to check here first before falling back to public PyPI for every other dependency.
COPY docker/vendor/ ./vendor/

RUN pip install --upgrade pip \
    && pip install -r requirements.txt --find-links vendor/ --no-input

# Production stage
FROM python:3.12-slim

WORKDIR /app

# Copy venv from builder
COPY --from=builder /app/venv /app/venv
ENV PATH="/app/venv/bin:$PATH"

# Copy application code
COPY src/ ./src/

# Copy entrypoint script and fix line endings (Windows CRLF -> Unix LF)
COPY docker/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN sed -i 's/\r$//' /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

# Create directories for configs and secrets
RUN mkdir -p /app/config /app/secrets

# Environment variables with defaults (can be overridden)
ENV HOST=0.0.0.0
ENV PORT=8005
ENV LOG_LEVEL=INFO

EXPOSE 8005

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health/live')" || exit 1

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "-m", "src.agent"]
