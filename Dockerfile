# syntax=docker/dockerfile:1
# Image multi-arch (linux/arm64 pour Raspberry Pi 4, linux/amd64).
# Images de base épinglées par digest : Dependabot ouvre une PR à chaque correctif publié.
FROM ghcr.io/astral-sh/uv:0.13@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 AS uv

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159 AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12 \
    UV_CONCURRENT_DOWNLOADS=2 UV_HTTP_TIMEOUT=120
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159
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
