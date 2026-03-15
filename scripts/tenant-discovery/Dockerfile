FROM python:3.12-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim AS runtime

RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

WORKDIR /app

COPY --from=builder /install /usr/local
COPY app/ ./app/

# Healthcheck for container
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD python -c "import os, sys; sys.exit(0 if os.path.exists('/tmp/healthcheck') else 1)"

USER appuser

CMD ["python", "-m", "app.main"]
