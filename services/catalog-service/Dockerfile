# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_DEV=1
WORKDIR /app/services/catalog-service
COPY services/catalog-service/pyproject.toml services/catalog-service/uv.lock ./
COPY libs/observability/pyproject.toml /app/libs/observability/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-local
COPY libs/observability /app/libs/observability
COPY services/catalog-service/ /app/services/catalog-service/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:3.12-slim-trixie AS runtime
RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot
WORKDIR /app/services/catalog-service
COPY --from=builder --chown=999:999 /app/.venv /app/.venv
COPY --from=builder --chown=999:999 /app/services/catalog-service /app/services/catalog-service
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER nonroot
EXPOSE 8000
LABEL org.opencontainers.image.title="kubecommerce-catalog" \
      org.opencontainers.image.source="https://github.com/bipulprasadjain007/kubecommerce" \
      org.opencontainers.image.licenses="MIT"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "25"]
