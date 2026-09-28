# Development guide

Engineering conventions every KubeCommerce service follows. Service implementations must be
consistent with this document; it is the contract that keeps five independently built services
interoperable.

## 1. Repository model

- Each library and service is an **independent uv project** with its own `pyproject.toml`,
  `uv.lock` and `.venv`. There is no uv workspace.
- Services depend on local libraries through `[tool.uv.sources]` path dependencies:
  ```toml
  dependencies = ["kubecommerce-observability"]
  [tool.uv.sources]
  kubecommerce-observability = { path = "../../libs/observability" }
  ```
  (`kubecommerce-contracts = { path = "../../libs/contracts" }` only for order-service and
  notification-worker.)
- All commands run from the project directory:
  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  cd services/<name>
  uv sync
  uv run pytest
  ```
- Root `ruff.toml` is shared via `[tool.ruff] extend = "../../ruff.toml"`.
- Do not modify `README.md`, `docs/**` (except this file's owner), `Makefile`, `scripts/**`,
  `libs/**` or another service while implementing a service.

## 2. Service anatomy

```text
services/<name>/
├── app/
│   ├── __init__.py
│   ├── main.py            # create_app() + module-level `app`
│   ├── config.py          # Settings(BaseServiceSettings) + get_settings()
│   ├── db.py              # engine/session factory (DB services)
│   ├── models.py          # SQLAlchemy models (DB services)
│   ├── schemas.py         # Pydantic request/response models
│   ├── repository.py      # data access (DB services)
│   ├── service.py         # business logic (when it adds clarity)
│   ├── clients/           # outbound HTTP/AMQP collaborators
│   └── api/
│       ├── __init__.py
│       ├── deps.py        # dependencies (session, auth, internal token)
│       └── routes.py      # APIRouter with the public endpoints
├── alembic/               # DB services only
│   ├── env.py
│   ├── script.py.mako
│   └── versions/0001_*.py
├── alembic.ini
├── tests/
│   ├── conftest.py
│   └── unit/…
├── pyproject.toml
├── uv.lock
└── Dockerfile
```

`pyproject.toml` baseline (adjust deps per service):

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "kubecommerce-<name>-service"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [...]

[dependency-groups]
dev = ["pytest", "pytest-asyncio", "pytest-cov", "httpx", "asgi-lifespan", "ruff", "mypy"]

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.ruff]
extend = "../../ruff.toml"

[tool.mypy]
python_version = "3.12"
mypy_path = "."
plugins = ["pydantic.mypy"]
disallow_untyped_defs = true
check_untyped_defs = true
no_implicit_optional = true
warn_redundant_casts = true
warn_unused_ignores = true
strict_equality = true

[[tool.mypy.overrides]]
module = ["tests.*"]
disallow_untyped_defs = false

[tool.pytest.ini_options]
pythonpath = ["."]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
asyncio_default_test_loop_scope = "function"
addopts = "-q --strict-markers"
markers = ["integration: requires external services (docker compose or testcontainers)"]
```

Never set `readme` in `[project]` — `*.md` files are excluded from image build contexts.

## 3. Application factory contract

```python
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

from fastapi import FastAPI

from kubecommerce_observability import install_observability
from app.api.routes import router
from app.config import Settings, get_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # create engines/clients/consumer tasks; store them on app.state
        yield
        # dispose resources, cancel background tasks, shutdown_tracing()

    app = FastAPI(
        title=settings.service_name,
        version=settings.service_version,
        lifespan=lifespan,
    )
    metrics = install_observability(
        app,
        settings=settings,
        readiness_checks=[("database", db_ready)],
        startup_checks=[],
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.include_router(router)
    return app


app = create_app()
```

Rules:

- One module-level `app` object (`app.main:app`) for uvicorn.
- `install_observability` is the **only** place that registers logging, middleware, metrics,
  health routes, `/metrics` and error handlers. Never hand-wire middleware.
- Readiness checks are `async () -> bool`, must not raise, and must be cheap (connection ping).
- Readiness is per service: auth/catalog/order check the database only; the notification
  worker checks the RabbitMQ connection (its core function) but NOT Redis; the gateway is
  dependency-free (rate limiting is deliberately fail-open and JWKS is cached) and surfaces
  dependency state via `dependency_up{dependency}` gauges plus transition logs.
- Redis (cache, dedupe, rate limiting) and, for order-service, the RabbitMQ broker are
  **degraded, not fatal**: never fail readiness on them; expose metrics/logs instead.
- Blocking startup work (migrations) is not done in lifespan; migrations run as an explicit Job.

## 4. Settings

- Subclass `BaseServiceSettings`; set `service_name` default to the canonical slug.
- Field names map to uppercase env vars (`auth_database_url` -> `AUTH_DATABASE_URL`).
- `get_settings()` is cached; tests construct `Settings(...)` directly and pass it to
  `create_app(settings)`.
- `environment` defaults to `dev`; secrets never have defaults in code.

## 5. Observability conventions

- Logging: `from kubecommerce_observability import get_logger`; structured events with
  snake_case names (`order_created`, `notification_sent`). Never log passwords, tokens, API keys
  or full auth headers. Prefer IDs over PII.
- Correlation: handled by middleware; outbound calls made with `create_http_client` carry
  `X-Correlation-ID` automatically.
- Custom metrics: `metrics = app.state.metrics`, then
  `metrics.counter("cache_hits_total", "…")`, `metrics.gauge(...)`, `metrics.histogram(...)`.
  Metric names must not contain hyphens (the namespace sanitizes the service slug).
  Never use unbounded label values (raw paths, user ids, emails, UUIDs).
- Standard RED metrics come from the bootstrap; use route templates only.

## 6. Error handling

```python
from kubecommerce_observability import ApiError

raise ApiError("insufficient_stock", "Not enough stock for product X", status_code=409)
```

- Error codes are lower_snake_case and stable (clients branch on them).
- `details` must be client-safe: never raw driver messages, DSNs or stack traces.
- Do not leak existence of other users' resources: return 404, not 403.
- Unhandled exceptions become `500 internal_error` with a correlation id; never hand-roll them.

## 7. Outbound HTTP

```python
from kubecommerce_observability import create_http_client

client = create_http_client(base_url=settings.some_base_url, timeout=5.0)
response = await client.get("/products/123")
```

- Always set a timeout. Translate `httpx.TimeoutException` -> `ApiError(..., 504)`,
  `httpx.RequestError` -> `ApiError(..., 502)`.
- Retry only idempotent requests (GET/HEAD), max 2 attempts, exponential backoff with jitter.
- Never retry `POST /internal/stock/reserve` or `/release` automatically; those are called
  exactly once per step and compensated explicitly on failure.
- Internal service-to-service mutation calls send `X-Internal-Token` from settings.

## 8. Database and migrations

- SQLAlchemy 2.0 async (`postgresql+asyncpg://` in every environment except tests).
- Portable models only: `Uuid(as_uuid=True)`, `DateTime(timezone=True)`, Python-side defaults
  `default=lambda: datetime.now(UTC)`. No PostgreSQL-only types (tests run on SQLite).
- Session dependency:
  ```python
  async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
      async with request.app.state.session_factory() as session:
          try:
              yield session
          except Exception:
              await session.rollback()
              raise
  ```
- Writes commit explicitly in the repository/service layer; read-only handlers never commit.
- Alembic: async `env.py` reads the service `*_DATABASE_URL` (never hard-code it in
  `alembic.ini`); one hand-written initial migration matching the models; migrations run by an
  explicit Kubernetes Job (`alembic upgrade head`), never at application startup.

## 9. Events (order-service producer, notification-worker consumer)

Use `kubecommerce_contracts` exclusively — no duplicated queue names or hand-built headers:

- Exchange `EXCHANGE_EVENTS` (durable topic), routing key `ROUTING_ORDER_CREATED`.
- Envelope: `new_order_created_event(OrderCreatedData(...), correlation_id, producer)`.
- AMQP headers: `event_headers(event, retry_count=N, traceparent=None)`.
- Publish with persistent delivery (`delivery_mode=2`) and **publisher confirms**; only treat a
  publish as successful after the broker confirms.
- Tiered retries: `retry_routing_key(attempt)` -> queue with `retry_queue_arguments(index)`
  (TTL then dead-letter back to the primary), `MAX_RETRIES`, then `ROUTING_ORDER_CREATED_DLQ`.
- At-least-once delivery: duplicates are expected; consumers dedupe by `event_id`.

### Outbox (order-service)

- The event row is inserted in the **same transaction** as the order.
- A background poller publishes pending rows and marks `published_at` only after broker confirm.
- Failures increment `attempts` and set `next_attempt_at` with capped exponential backoff;
  order creation must succeed even while the broker is unavailable.

### Idempotency

- `POST /orders` honors `Idempotency-Key`: unique index; replay returns the original order.
- Worker dedupe protocol: `SET dedupe:{event_id} processing NX EX <ttl>`; on success set `done`;
  on failure `DEL` the key so a retry can reprocess. Never mark done before the side effect.

## 10. Testing rules

- Unit tests run with **no external services**:
  - DB: SQLite file via `aiosqlite` (`tmp_path`), `Base.metadata.create_all`, no Alembic;
  - Redis: `fakeredis.aioredis.FakeRedis`;
  - HTTP collaborators: `respx` on `httpx` clients passed into the app;
  - RabbitMQ: inject fake `publish` callables; never construct a real connection.
- FastAPI tests: `httpx.ASGITransport(app=app)`. `ASGITransport` does **not** run lifespan —
  wrap with `asgi_lifespan.LifespanManager` when the app needs lifespan state.
- JWT tests generate a throwaway RSA keypair with `cryptography`; never commit keys.
- Coverage gate: `--cov=app --cov-fail-under=80`, critical paths always covered.
- Tests must be deterministic; no sleeps longer than a few ms, no network.

## 11. Dockerfile template (adapted per service)

Only copy the library sections the service actually uses (all use observability; order-service
and notification-worker also use contracts).

```dockerfile
# syntax=docker/dockerfile:1
FROM python:3.12-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_NO_DEV=1
WORKDIR /app/services/<name>
COPY services/<name>/pyproject.toml services/<name>/uv.lock ./
COPY libs/observability/pyproject.toml /app/libs/observability/pyproject.toml
COPY libs/contracts/pyproject.toml /app/libs/contracts/pyproject.toml
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-local
COPY libs/observability /app/libs/observability
COPY libs/contracts /app/libs/contracts
COPY services/<name>/ /app/services/<name>/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-editable

FROM python:3.12-slim-trixie AS runtime
RUN groupadd --system --gid 999 nonroot \
 && useradd --system --gid 999 --uid 999 --create-home nonroot
WORKDIR /app/services/<name>
COPY --from=builder --chown=999:999 /app/.venv /app/.venv
COPY --from=builder --chown=999:999 /app/services/<name> /app/services/<name>
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
USER nonroot
EXPOSE 8000
LABEL org.opencontainers.image.title="kubecommerce-<name>" \
      org.opencontainers.image.source="https://github.com/your-github-username/kubecommerce" \
      org.opencontainers.image.licenses="MIT"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--no-access-log", "--timeout-graceful-shutdown", "25"]
```

Notes:

- `--locked` (not `--frozen`) so stale lockfiles fail the build.
- `uv sync --no-install-local` first installs third-party dependencies only; the local libs are
  installed by the second sync with `--no-editable`.
- Never use `uv run` in the runtime image (requires a writable cache); invoke `uvicorn` directly.
- The gateway additionally passes `--proxy-headers --forwarded-allow-ips=*` (in-cluster only;
  NetworkPolicies restrict exposure).
- Never deploy `latest`; CI tags by immutable Git SHA.

## 12. Canonical slugs, ports and env

| Service | slug (`service_name`) | container port | compose host port |
|---|---|---|---|
| gateway-api | `gateway-api` | 8000 | 8080 |
| auth-service | `auth-service` | 8000 | 8001 |
| catalog-service | `catalog-service` | 8000 | 8002 |
| order-service | `order-service` | 8000 | 8003 |
| notification-worker | `notification-worker` | 8000 | 8004 |

Full environment-variable list: `.env.example` (groups per service; shared vars are
`ENVIRONMENT`, `LOG_LEVEL`, `SERVICE_VERSION`, `OTEL_*`, `SERVICE_NAME`).

## 13. Definition of done for a service

```bash
export PATH="$HOME/.local/bin:$PATH"
cd services/<name>
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run pytest -q --cov=app --cov-report=term-missing --cov-fail-under=80
```

- `uv.lock` exists and is committed with the service.
- Tests pass without Docker or any external service.
- `Dockerfile` follows the template above.
- No secrets, tokens or credentials in code, tests or logs.
