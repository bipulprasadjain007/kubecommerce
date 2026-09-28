# KubeCommerce - Architecture

> Source of truth: `project_1_kubernetes_cicd_microservices.md`. This document
> explains the application and delivery architecture, the trust boundaries, the
> API surface, the event contract, and the configuration model.

## 1. Component responsibilities

| Component | Responsibility | Data ownership | Runtime |
|---|---|---|---|
| `gateway-api` | Single public entry point; verifies JWTs; attaches/propagates `X-Correlation-ID`; sets `X-User-ID`; Redis-backed rate limiting; returns the consistent error schema | No persistent DB (Redis for rate-limit state only) | FastAPI, container port `8000` |
| `auth-service` | User registration, login, JWT (RS256) issuance, JWKS publication, `/me` | `auth_db` (PostgreSQL) | FastAPI, port `8000` |
| `catalog-service` | Product catalog + inventory state; Redis read-through cache invalidation on writes | `catalog_db` (PostgreSQL) + Redis cache | FastAPI, port `8000` |
| `order-service` | Create/read orders; validates inventory against catalog; transactional outbox publishing `order.created` | `orders_db` (PostgreSQL), outbox table | FastAPI, port `8000` |
| `notification-worker` | Consumes `order.created`; idempotent by `event_id`; retries with backoff; DLQ; simulated webhook/structured log | Event queue only (Redis for idempotency keys) | Python worker + small health/metrics HTTP server, port `8000` |

Shared libraries (independent uv packages):

- `libs/contracts` (`kubecommerce_contracts`) - the `order.created` event envelope and RabbitMQ topic/queue constants.
- `libs/observability` (`kubecommerce_observability`) - structured logging, Prometheus metrics, OpenTelemetry tracing, health/readiness helpers, error handlers, settings.

## 2. Service diagram

```mermaid
flowchart TD
    U[Client / k6 load test] --> GW[Gateway API / Load Balancer]
    GW --> API[gateway-api]
    API --> AUTH[auth-service]
    API --> CAT[catalog-service]
    API --> ORD[order-service]

    AUTH --> PG1[(auth_db)]
    CAT --> PG2[(catalog_db)]
    ORD --> PG3[(orders_db)]

    API --> REDIS[(Redis)]
    ORD --> MQ[(RabbitMQ)]
    MQ --> WORKER[notification-worker]

    API -. OTLP .-> OTEL[OpenTelemetry Collector]
    AUTH -. OTLP .-> OTEL
    CAT -. OTLP .-> OTEL
    ORD -. OTLP .-> OTEL
    WORKER -. OTLP .-> OTEL

    OTEL --> TEMPO[Tempo]
    APPMETRICS[Service /metrics endpoints] --> PROM[Prometheus]
    K8S[Kubernetes metrics] --> PROM
    LOGS[Container logs] --> LOKI[Loki]
    PROM --> GRAF[Grafana]
    TEMPO --> GRAF
    LOKI --> GRAF
    PROM --> ALERT[Alertmanager]
```

## 3. Delivery architecture

```mermaid
flowchart LR
    DEV[Developer] --> GH[GitHub application repo]
    GH --> CI[GitHub Actions CI]
    CI --> TEST[Unit + integration + security checks]
    TEST --> BUILD[Docker Buildx]
    BUILD --> REG[GHCR or Amazon ECR]
    REG --> SIGN[SBOM + provenance/signature]
    CI --> GITOPS[GitOps repo image-tag PR/update]
    GITOPS --> ARGO[Argo CD]
    ARGO --> K8S[Kubernetes dev/staging/prod]
```

CI builds and publishes artifacts; Argo CD reconciles deployment state from Git.
CI never needs broad `kubectl` credentials to the production cluster.

## 4. Data ownership

Each service owns its schema and no service reads another service's tables:

| Database | Owner | Notes |
|---|---|---|
| `auth_db` | `auth-service` | users, credentials, refresh/denylist state if added |
| `catalog_db` | `catalog-service` | products, inventory |
| `orders_db` | `order-service` | orders, order items, outbox |

For a low-cost portfolio, **one PostgreSQL instance hosts three separate databases**
under separate logical ownership (`auth_db`, `catalog_db`, `orders_db`). A stricter
production architecture would give each service its own instance/cluster once
operational requirements (blast radius, scaling, compliance) justify the cost. Redis
logical databases are likewise separated by purpose (`/0` catalog, `/1` worker, `/2` gateway).

## 5. Request flows (with correlation IDs)

A correlation ID is created at the edge (client or gateway), propagated on every hop
via the `X-Correlation-ID` header, embedded in structured logs, added as a span
attribute, and echoed in error responses.

### 5.1 Register / login

```text
Client -> gateway POST /api/auth/users -> auth POST /users        (hash password, persist user)
Client -> gateway POST /api/auth/login -> auth POST /login        (verify, issue RS256 JWT, return token)
Gateway serves GET /api/auth/me by validating the bearer token then calling auth GET /me
Auth publishes GET /.well-known/jwks.json (public key set) for gateway JWKS verification
```

### 5.2 Create product (internal mutation)

```text
Operator/CI -> catalog POST /products with X-Internal-Token
            -> validate token, persist product, invalidate/invalidate-on-read Redis cache
Public reads: Client -> gateway GET /api/catalog/products[/{id}] -> catalog (read-through Redis cache)

Internal inventory reservations (order flow) use POST /internal/stock/reserve and
POST /internal/stock/release (compensation); both are X-Internal-Token protected and atomic.
The cache is versioned and invalidated after every write; Redis failures are fail-open.
```

### 5.3 Create order -> outbox -> RabbitMQ -> worker

```text
Client -> gateway POST /api/orders (Bearer)
   gateway: verify JWT (JWKS), rate-limit via Redis, set X-User-ID + X-Correlation-ID
        -> order POST /orders
   order:  validate schema -> GET catalog /products/{id} for price/sku (internal token)
        -> reserve each item atomically via POST /internal/stock/reserve
           (on later failure: release already-reserved items -> compensation)
        -> in ONE transaction: insert order + items + outbox row (event payload)
        -> return order id + correlation id (Idempotency-Key replays return the original order)
   order outbox publisher (poll loop):
        -> SELECT unsent outbox rows -> publish order.created (publisher confirms)
        -> mark row sent
RabbitMQ -> notification-worker consumes order.created
        -> dedupe claim by event_id (SET NX processing 60s; done marker 7d)
        -> process (webhook if WORKER_WEBHOOK_URL set, else structured log)
        -> ack; failures move through fixed retry tiers (5s/30s/120s) then DLQ;
           a duplicate in-flight claim is rescheduled, never ack-dropped
```

At-least-once delivery is made safe by outbox + idempotent consumption (see section 8).

## 6. Trust boundaries

- **`gateway-api` is the only publicly reachable component.** PostgreSQL, Redis,
  RabbitMQ, and all internal services are private.
- **The gateway authenticates callers** and sets `X-User-ID` to the verified subject.
  Downstream services treat `X-User-ID` as authoritative *only* because they are not
  publicly reachable. A future hardened step adds NetworkPolicy so only the gateway can
  reach auth/catalog/order, and order-service can reach catalog.
- **Internal mutations require `X-Internal-Token`** (per-service shared token) on top of a
  trusted network position: `POST /products`, `PATCH /products/{id}/stock`, the internal
  stock reserve/release endpoints, and all order-service routes (the gateway attaches the
  order token on the order leg). Inventory writes are intentionally **not** exposed through
  the public gateway because there is no role model yet.
- **NetworkPolicy: Status: planned (Phase 4/12).** Default-deny ingress/egress with
  explicit allows is the target state, but does nothing unless the CNI enforces it.

```text
                 public            private (ClusterIP only)
Client --> [Gateway API/LB] --> gateway-api --> auth-service
                                          --> catalog-service
                                          --> order-service --> catalog-service
                                          --> RabbitMQ --> notification-worker
                          all services --> PostgreSQL / Redis
```

## 7. API surface per service

Error responses everywhere use the consistent schema:

```json
{
  "error": {
    "code": "ORDER_INVENTORY_UNAVAILABLE",
    "message": "Insufficient stock for one or more items",
    "correlation_id": "5f1c...",
    "details": { "product_id": "..." }
  }
}
```

`details` is optional and must never contain secrets or credentials.

### gateway-api (public; only externally exposed service)

| Method | Path | Auth | Backend |
|---|---|---|---|
| GET | `/health/live` | none | local |
| GET | `/health/ready` | none | local (dependency-free; Redis/auth surfaced via `dependency_up` gauges) |
| GET | `/metrics` | none (cluster-internal scrape) | local |
| POST | `/api/auth/users` | none | auth `POST /users` |
| POST | `/api/auth/login` | none | auth `POST /login` |
| GET | `/api/auth/me` | Bearer | auth `GET /me` |
| GET | `/api/catalog/products` | none | catalog `GET /products` |
| GET | `/api/catalog/products/{id}` | none | catalog `GET /products/{id}` |
| POST | `/api/orders` | Bearer (+ `X-Internal-Token` to order) | order `POST /orders` |
| GET | `/api/orders` | Bearer (subject from token, not query) | order `GET /orders` |
| GET | `/api/orders/{id}` | Bearer (ownership enforced) | order `GET /orders/{id}` |

Catalog mutations (`POST /products`, `PATCH /stock`) are deliberately **not** exposed by the
gateway; they are internal-only and require `X-Internal-Token`.

The gateway adds `X-Correlation-ID` if absent, enforces `GATEWAY_RATE_LIMIT_PER_MINUTE`
via Redis, applies `GATEWAY_REQUEST_TIMEOUT_SECONDS`, and returns the error schema.

### auth-service (private)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/users` | none (reached via gateway) | register; passwords hashed (argon2) |
| POST | `/login` | none | returns short-lived access token |
| GET | `/me` | Bearer | requires `X-User-ID` or validates bearer |
| GET | `/.well-known/jwks.json` | none | public key set for gateway |
| GET | `/health/live`, `/health/ready`, `/metrics` | none | readiness checks `auth_db` |

### catalog-service (private)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/products` | none (private network) | read-through Redis cache |
| GET | `/products/{id}` | none | cached |
| POST | `/products` | `X-Internal-Token` | create product; invalidates cache |
| PATCH | `/products/{id}/stock` | `X-Internal-Token` | atomic conditional inventory update; invalidates cache |
| POST | `/internal/stock/reserve` | `X-Internal-Token` | atomic reserve for order-service (409 on insufficient stock) |
| POST | `/internal/stock/release` | `X-Internal-Token` | compensation release for failed order attempts |
| GET | `/health/live`, `/health/ready`, `/metrics` | none | readiness checks DB only; Redis cache fail-open |

### order-service (private)

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/orders` | `X-User-ID` + `X-Internal-Token` (set by gateway) | reserves stock via catalog with compensation; transactional outbox; honors `Idempotency-Key`; concurrent same-key races replay the winner |
| GET | `/orders/{id}` | `X-User-ID` | ownership enforced (404 for other users' orders) |
| GET | `/orders?limit&offset` | `X-User-ID` | list for the caller, newest first |
| GET | `/health/live`, `/health/ready`, `/metrics` | none | readiness checks DB; broker degraded via outbox (`outbox_pending_events` metric) |

### notification-worker (private)

| Method | Path | Auth | Notes |
|---|---|---|---|
| GET | `/health/live` | none | process alive |
| GET | `/health/ready` | none | RabbitMQ connection open (broker is the worker's core dependency; Redis is degraded) |
| GET | `/metrics` | none | consume success/failure, DLQ counters |
| (consumer) | `order.created` queue | n/a | idempotent by `event_id`; retry + DLQ |

## 8. Event contract: `order.created`

Versioned, explicit envelope (defined in `libs/contracts`):

The authoritative models live in `libs/contracts/kubecommerce_contracts/events.py`
(`OrderCreatedEvent`, `OrderCreatedData`, `OrderItem`), all strict
(`extra="forbid"`) so malformed payloads fail fast at the service boundary.

Matching example (all ids are UUIDs; `created_at`/`occurred_at` are
timezone-aware UTC):

```json
{
  "event_type": "order.created",
  "event_version": 1,
  "event_id": "b3c1e2f0-1a2b-4c3d-8e4f-5a6b7c8d9e0f",
  "occurred_at": "2026-09-25T12:34:56.789Z",
  "correlation_id": "5f1c9a2b3c4d",
  "producer": "order-service",
  "data": {
    "order_id": "0f8e7d6c-5b4a-4938-8271-6a5b4c3d2e1f",
    "user_id": "1a2b3c4d-5e6f-4071-8293-a4b5c6d7e8f9",
    "items": [
      { "product_id": "9c8b7a6d-5e4f-4132-8a79-b0c1d2e3f405", "sku": "SKU-0001", "quantity": 2, "unit_price_cents": 1999 }
    ],
    "total_cents": 3998,
    "currency": "USD",
    "created_at": "2026-09-25T12:34:56.789Z"
  }
}
```

- `event_type` + `event_version` allow schema evolution; consumers reject unknown
  major versions rather than guessing.
- `event_id` is the idempotency key.
- `occurred_at` / `created_at` are RFC 3339 UTC; timezone-naive values are rejected.
- `correlation_id` (1-128 chars) ties the event back to the originating HTTP request.

### RabbitMQ topology (from `libs/contracts`)

| Constant | Value |
|---|---|
| Exchange | `kubecommerce.events` (durable topic) |
| Routing key | `order.created` |
| Consumer queue | `kubecommerce.notifications.order-created` |
| Retry queues | `...retry.1`, `...retry.2`, `...retry.3` |
| Retry delays (ms) | `5000`, `30000`, `120000` |
| Retry header | `x-retry-count` |
| Dead-letter queue | `kubecommerce.notifications.order-created.dlq` |

### At-least-once + idempotency design

1. **Transactional outbox (order-service).** The order row, order items, and the
   `order.created` payload are written in the *same* database transaction. A background
   publisher polls (`ORDERS_OUTBOX_POLL_INTERVAL_SECONDS`) for unsent rows, publishes
   with publisher confirms, then marks them sent. This avoids the classic
   "DB committed but message lost" failure and guarantees the event is eventually sent.
2. **At-least-once.** A crash after publish but before the "sent" update re-publishes;
   consumers must therefore tolerate duplicates.
3. **Idempotent consumer (notification-worker).** Before processing, it claims
   `dedupe:{event_id}` in Redis with `SET ... processing NX EX 60s`; on success the marker
   becomes `done` (retained 7 days), on failure it is deleted so a retry can reprocess.
   Only a `done` marker is treated as "already handled" -> ack without side effects. A
   duplicate `processing` claim (for example from a crashed pod) is rescheduled through the
   retry tiers with the claim left intact, so the message is never ack-dropped.
4. **Tiered retries + DLQ.** Failures are republished to `order.created.retry.1..3`
   (5 s / 30 s / 120 s TTL queues that dead-letter back to the primary queue); after
   `WORKER_MAX_RETRIES` the message is published to routing key `order.created.dead` (DLQ).
   Poison messages (invalid or unsupported schema) go straight to the DLQ.
5. **Idempotency keys** on order creation are implemented: `POST /orders` honors
   `Idempotency-Key` (unique index); replays return the original order.
   (**Status: implemented**)

## 9. JWT design

- **Algorithm:** RS256 (asymmetric). `auth-service` holds the private key and signs;
  the gateway verifies with the public key fetched from JWKS - it never needs the
  private key.
- **Keys:** loaded from `AUTH_JWT_PRIVATE_KEY_FILE` / `AUTH_JWT_PUBLIC_KEY_FILE`
  (or the inline `AUTH_JWT_PRIVATE_KEY` / `AUTH_JWT_PUBLIC_KEY` alternatives).
  Local keys are generated into `.secrets/` by `scripts/dev-bootstrap.sh`; in AWS they
  come from Secrets Manager via External Secrets Operator.
- **Claims:**

| Claim | Meaning |
|---|---|
| `sub` | user id (becomes `X-User-ID`) |
| `iss` | `AUTH_JWT_ISSUER` (default `kubecommerce-auth`) |
| `aud` | `AUTH_JWT_AUDIENCE` (default `kubecommerce`) |
| `iat` / `exp` | issued-at / expiry (`AUTH_ACCESS_TOKEN_TTL_SECONDS`, default 900) |
| `jti` | token id (supports future denylisting) |
| `typ` | `access` |

- **Verification:** gateway validates signature via JWKS, then `iss`, `aud`, `exp`, and
  clock skew; JWKS responses can be cached to avoid per-request fetches.
- **Rotation:** rotate the signing key by adding the new key to JWKS, letting old
  tokens expire, then removing the old key (**Status: documented procedure in runbook; rotate demo planned Phase 6.3**).

## 10. Environment design

| Environment | Purpose | Runtime |
|---|---|---|
| `dev` | Fast developer validation | kind/k3d or a shared EKS namespace |
| `staging` | Production-like integration/release validation | kind/k3d initially, EKS later |
| `prod` | Controlled release demonstration | EKS (Phase 13/later) |

Local low-cost model: `dev` and `staging` are **namespaces in one kind cluster**
(`kubecommerce-dev`, `kubecommerce-staging`); `prod` is a namespace
(`kubecommerce-prod`) locally and maps to EKS later. Stronger isolation means separate
clusters/accounts in the cloud even if one cluster is used for the demonstration.

## 11. Naming conventions

- **Container images:** `ghcr.io/<user>/kubecommerce-<service>:<git-sha>`
  (e.g. `ghcr.io/your-github-username/kubecommerce-order-service:a1b2c3d4e5f6`).
  A mutable `latest-dev` tag may exist locally only; production deploys immutable
  SHA/digest tags. Never deploy `latest` to production.
- **Namespaces:** `kubecommerce-dev`, `kubecommerce-staging`, `kubecommerce-prod`
  plus `platform-system`, `monitoring`, `observability`, `argocd`.
- **Kubernetes labels:**

| Label | Example |
|---|---|
| `app.kubernetes.io/name` | `order-service` |
| `app.kubernetes.io/component` | `api` / `worker` |
| `app.kubernetes.io/part-of` | `kubecommerce` |
| `app.kubernetes.io/version` | `0.1.0` or the image tag |
| `environment` | `dev` / `staging` / `prod` |

- **Resource names:** `<service>` for Deployment/Service, `<service>-config` for ConfigMap,
  `<service>-secrets` for Secret, `<service>` ServiceAccount.

## 12. Dependency management

Each library and service is a **standalone uv project** with its own `pyproject.toml`,
`uv.lock`, and `.venv`. Services depend on libraries through
`[tool.uv.sources]` path dependencies, for example:

```toml
# services/order-service/pyproject.toml
[project]
dependencies = [
  "kubecommerce-contracts",
  "kubecommerce-observability",
  # ...
]

[tool.uv.sources]
kubecommerce-contracts = { path = "../../libs/contracts" }
kubecommerce-observability = { path = "../../libs/observability" }
```

Rationale: avoids top-level `app` package collisions across services in a shared
workspace venv, keeps each service independently buildable/containerizable, and lets
`make install`/`make test` iterate per project. Dependencies are pinned via each
project's `uv.lock`; updates go through deliberate dependency-automation PRs.

## 13. Configuration / environment variables

Convention: **the settings field name equals the uppercase environment variable name**
(e.g. `.environment` <-> `ENVIRONMENT`). Real secrets are never committed; `.env.example`
is the documented template and `.env` is git-ignored.

### Shared (all services)

| Variable | Default | Purpose |
|---|---|---|
| `ENVIRONMENT` | `dev` | `dev` / `staging` / `prod`; used in logs, metrics, tracing resource |
| `LOG_LEVEL` | `INFO` | structured log level |
| `SERVICE_VERSION` | `0.1.0` | reported in logs/metrics/health |
| `OTEL_ENABLED` | `false` | enable OTLP trace/metric export |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4318` | OTLP HTTP endpoint |
| `OTEL_SAMPLE_RATIO` | `1.0` | trace sampling ratio |

### auth-service

| Variable | Example |
|---|---|
| `AUTH_DATABASE_URL` | `postgresql+asyncpg://kubecommerce:devpassword@postgres:5432/auth_db` |
| `AUTH_JWT_PRIVATE_KEY_FILE` | `/secrets/jwt-private.pem` |
| `AUTH_JWT_PRIVATE_KEY` | inline PEM (alternative to the file) |
| `AUTH_JWT_PUBLIC_KEY_FILE` | `/secrets/jwt-public.pem` |
| `AUTH_JWT_PUBLIC_KEY` | inline PEM (alternative) |
| `AUTH_JWT_ISSUER` | `kubecommerce-auth` |
| `AUTH_JWT_AUDIENCE` | `kubecommerce` |
| `AUTH_ACCESS_TOKEN_TTL_SECONDS` | `900` |

### catalog-service

| Variable | Example |
|---|---|
| `CATALOG_DATABASE_URL` | `postgresql+asyncpg://.../catalog_db` |
| `CATALOG_REDIS_URL` | `redis://redis:6379/0` |
| `CATALOG_CACHE_TTL_SECONDS` | `60` |
| `CATALOG_INTERNAL_API_TOKEN` | `dev-internal-token-change-me` |

### order-service

| Variable | Example |
|---|---|
| `ORDERS_DATABASE_URL` | `postgresql+asyncpg://.../orders_db` |
| `ORDERS_RABBITMQ_URL` | `amqp://kubecommerce:devpassword@rabbitmq:5672/` |
| `ORDERS_CATALOG_BASE_URL` | `http://catalog-service:8000` |
| `ORDERS_INTERNAL_API_TOKEN` | `dev-internal-token-change-me` |
| `ORDERS_REQUEST_TIMEOUT_SECONDS` | `5` |
| `ORDERS_OUTBOX_POLL_INTERVAL_SECONDS` | `2` |

### notification-worker

| Variable | Example |
|---|---|
| `WORKER_RABBITMQ_URL` | `amqp://kubecommerce:devpassword@rabbitmq:5672/` |
| `WORKER_REDIS_URL` | `redis://redis:6379/1` |
| `WORKER_WEBHOOK_URL` | empty = structured log only |
| `WORKER_MAX_RETRIES` | `3` |

### gateway-api

| Variable | Example |
|---|---|
| `GATEWAY_REDIS_URL` | `redis://redis:6379/2` |
| `GATEWAY_AUTH_BASE_URL` | `http://auth-service:8000` |
| `GATEWAY_CATALOG_BASE_URL` | `http://catalog-service:8000` |
| `GATEWAY_ORDER_BASE_URL` | `http://order-service:8000` |
| `GATEWAY_JWKS_URL` | `http://auth-service:8000/.well-known/jwks.json` |
| `GATEWAY_JWT_ISSUER` | `kubecommerce-auth` |
| `GATEWAY_JWT_AUDIENCE` | `kubecommerce` |
| `GATEWAY_RATE_LIMIT_PER_MINUTE` | `120` |
| `GATEWAY_REQUEST_TIMEOUT_SECONDS` | `5` |
| `GATEWAY_INTERNAL_API_TOKEN` | `dev-internal-token-change-me` |

### Infrastructure / Compose

| Variable | Example |
|---|---|
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | `kubecommerce` / `devpassword` / `postgres` |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` | `kubecommerce` / `devpassword` |
| `COMPOSE_PROJECT_NAME` | `kubecommerce` |
| `IMAGE_REGISTRY` | `ghcr.io/your-github-username` |
| `IMAGE_TAG` | `dev` |

See `.env.example` for the complete annotated template.
