# syntax=docker/dockerfile:1
# Image multi-arch (linux/arm64 pour Raspberry Pi 4, linux/amd64).
FROM ghcr.io/astral-sh/uv:0.13 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12 \
    UV_CONCURRENT_DOWNLOADS=2 UV_HTTP_TIMEOUT=120
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim
RUN useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data /config && chown app:app /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris \
    DATA_DIR=/data \
    COMMUTE_CONFIG=/config/config.yaml
USER app
WORKDIR /app
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 \
    CMD ["python", "-m", "commute_alerts.health", "--check"]
ENTRYPOINT ["python", "-m", "commute_alerts"]
