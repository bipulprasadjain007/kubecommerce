# KubeCommerce - Runbook

Operational guide for local development, cluster operations, deployment/rollback,
database migrations, debugging, secret rotation, and incident triage.

> Tooling status in this working copy: Python + `uv` are installed. Docker, kind,
> kubectl, Helm, k6, trivy and pre-commit are **not** installed yet. Commands below
> that need them fail with a clear message until the tools exist. Phases are noted so
> it is clear what is implemented now versus planned.

## 1. Local development

```bash
# 1. One-time bootstrap: verify uv, generate .secrets/ JWT keys, create .env
bash scripts/dev-bootstrap.sh

# 2. Install deps for every uv project + git hooks
make install

# 3. Unit tests (no Docker required; integration tests are deselected)
make test

# 4. Lint / format / types
make lint
make typecheck
```

Run a single project directly:

```bash
cd services/auth-service
uv run pytest -q
uv run ruff check .
uv run mypy app
```

Per-project layout: each `libs/*` and `services/*` directory is its own uv project
with its own `.venv` created by `uv sync`.

## 2. Compose workflow (Phase C)

```bash
make compose-up         # docker compose up -d --build --wait (requires Docker + compose.yaml)
make smoke-test         # register -> login -> product -> order end-to-end
make compose-down       # docker compose down -v
make integration-test   # compose up + tests/integration (integration then failure suite)
```

Compose service names are the canonical slugs: `gateway-api`, `auth-service`,
`catalog-service`, `order-service`, `notification-worker`, plus `postgres`, `redis`,
`rabbitmq`, `webhook-sink` and the one-shot `migrate-auth` / `migrate-catalog` /
`migrate-orders` jobs. Compose host ports: gateway `8080`, auth `8001`, catalog
`8002`, order `8003`, worker `8004`, webhook sink `9000`; all application containers
listen on `8000`.

`make compose-up` waits for every healthcheck (with a 300 s wait timeout). Database
schemas are applied by the `migrate-*` one-shot services (Alembic `upgrade head`) before
their owning service starts; application startup never migrates.
`scripts/postgres/init-databases.sh` creates `auth_db`, `catalog_db` and `orders_db` on
first Postgres init.

Requirements: a Compose implementation >= 2.16 (`up --wait-timeout`, convergence with
one-shot services) and JWT keys from `bash scripts/dev-bootstrap.sh` - both
`make compose-up` and `make integration-test` preflight `.secrets/jwt-private.pem` and warn
when `.env` is missing.

Useful checks:

```bash
docker compose ps
docker compose logs -f gateway-api
docker compose logs -f notification-worker
docker compose logs migrate-auth
```

## 3. Local Kubernetes with kind (Phase 4)

`make kind-up` delegates to `scripts/kind-up.sh`, which bootstraps a local kind
cluster and installs the Phase 4 platform: Gateway API CRDs, Envoy Gateway and
metrics-server, plus the KubeCommerce namespaces. It is **idempotent** - an
existing cluster is reused and never deleted.

Prerequisites:

- Docker Engine/Desktop with a running daemon (`docker info` must succeed);
- `kind` (>= 0.30; the locally installed v0.34 is used);
- `kubectl` (v1.37);
- `helm` 3 (v3.22).

```bash
make kind-up                             # create/reuse cluster + install platform
make kind-up                             # re-run: safe, reuses the existing cluster
make k8s-secrets ENV=dev                 # create platform + app Secrets from .secrets/ + .env
bash scripts/kind-up.sh --with-argocd    # also run the Argo CD bootstrap (Phase 8)
```

`make k8s-secrets` creates `platform-system/kubecommerce-infra-secrets` (Postgres/Redis/RabbitMQ
passwords; generated once into the git-ignored `.secrets/k8s-<env>-passwords.env`) and the five
`<service>-secrets` consumed by the Helm chart. Re-running is idempotent; restart deployments to
pick up rotated values.

What `scripts/kind-up.sh` does:

1. preflight `docker` (daemon reachable), `kind`, `kubectl`, `helm` with clear
   install hints;
2. create the `kubecommerce` kind cluster (1 control-plane + 2 workers; host
   port `80` is mapped to the Envoy Gateway NodePort `30080` on the
   control-plane) unless it exists. Phase H adds host `443` -> container
   `30443` (TLS); the 443 mapping is intentionally absent until then, so
   everything below is plain HTTP on `http://localhost/`;
3. install Gateway API CRDs (pinned) and wait for `Established`;
4. install Envoy Gateway from its Helm OCI chart into `envoy-gateway-system` and
   wait for the controller deployment. The `GatewayClass`/`Gateway` are owned by
   the GitOps repo (`kubecommerce-gitops`, Phase D lane 3) and are **not**
   created here;
5. install metrics-server with `--kubelet-insecure-tls` (required on kind);
6. create the namespaces `kubecommerce-dev`, `kubecommerce-staging`,
   `kubecommerce-prod`, `platform-system`, `monitoring`, `observability`,
   `argocd`, all labelled `app.kubernetes.io/part-of=kubecommerce`. The
   `kubecommerce-*` namespaces also get `environment=<env>` and Pod Security
   Admission `pod-security.kubernetes.io/enforce=baseline` with
   `warn`/`audit=restricted`;
7. optionally (`--with-argocd`) run
   `kubecommerce-gitops/bootstrap/argocd/install.sh` when present - Argo CD is
   Phase 8, otherwise a note is printed.

**NetworkPolicy / CNI.** The default kind CNI (`kindnet`) does **not** enforce
`NetworkPolicy`, so any policy applied locally is inert. `kind-up` prints this
notice on every run. For a local cluster that *does* enforce policies, use the
opt-in Calico profile: it writes `networking.disableDefaultCNI: true` and
`networking.podSubnet: 10.244.0.0/16` into the generated kind config, then
installs pinned Calico (`v3.32.2`: operator manifest + `Installation` CR) and
waits for `calico-system` to become available before continuing:

```bash
CNI=calico make kind-up     # enforcing CNI; first run is slower (image pulls)
```

On a real cluster (EKS) use the VPC CNI with its network policy agent instead.
The node image defaults to `kindest/node:v1.36.4` (kind v0.33's shipped image):
`kube-prometheus-stack` is tested only through Kubernetes 1.36, so 1.37 is
intentionally not used. Override with `KIND_NODE_IMAGE` to change it.

Every external artifact is pinned and overridable:

```bash
CLUSTER_NAME=kubecommerce \
WORKERS=2 \
KIND_NODE_IMAGE=kindest/node:v1.36.4@sha256:099e049362a1526b2db71494e1947aae99bd16290d7c895f2b7ea312e3cbfaed \
CNI=kindnet \
CALICO_VERSION=v3.32.2 \
GATEWAY_API_VERSION=v1.6.2 \
ENVOY_GATEWAY_CHART_VERSION=v1.9.1 \
METRICS_SERVER_CHART_VERSION=3.14.0 \
  make kind-up
```

Health checks:

```bash
kubectl config current-context            # kind-kubecommerce
kubectl get nodes                         # 1 control-plane + 2 workers Ready
kubectl get ns                            # the seven namespaces listed above
kubectl -n envoy-gateway-system get deploy
kubectl -n kube-system get deploy metrics-server
kubectl top nodes                         # empty until metrics-server is ready
kubectl -n kubecommerce-dev get pods -o wide
kubectl -n kubecommerce-dev get gateway,httproute   # after the GitOps lane applies them
curl -sI http://localhost/                           # host 80 -> Envoy Gateway NodePort 30080
```

> Reliability and self-healing demos (rolling update, HPA, crash recovery,
> node disruption, rollback) are driven by `scripts/reliability-demo.sh` and
> documented in **section 10** below.

Delete the cluster when done (no-op with a message if it does not exist):

```bash
make kind-down
CLUSTER_NAME=<name> bash scripts/kind-down.sh   # delete a non-default cluster
```

> **Data loss.** `make kind-down` deletes the entire cluster, including every
> PersistentVolumeClaim - Postgres data is gone. On the next `make kind-up` +
> Helm install, Postgres re-runs `scripts/postgres/init-databases.sh` on its
> fresh volume. Back up anything you need before deleting.

### Known constraints (local platform)

- Argo CD sources are already set to
  `https://github.com/bipulprasadjain007/kubecommerce-gitops.git` in `argocd/root-app.yaml`,
  `argocd/applicationset.yaml`, `argocd/project.yaml` and every `platform/**` Application.
  If you fork the project, update all of them consistently or Argo rejects the sources.
- The Alembic migration Jobs are `pre-install`/PreSync hooks that reference only out-of-band
  Secrets. Under Argo CD the namespaces/infrastructure waves run first, so Postgres is live when
  they execute. On a **bare `helm install`** without the platform stack, wait for Postgres first
  or re-run the release (the Jobs have a limited `backoffLimit`).
- `ORDERS_INTERNAL_API_TOKEN` is used both for inbound order-service validation and for
  order-service -> catalog calls; keep it equal to `CATALOG_INTERNAL_API_TOKEN` (the local secrets
  script aligns them).
- The locally installed `kind` CLI is 0.34.0-alpha while the pinned node image is kind v0.33's
  `kindest/node:v1.36.4` digest; if `kind create` rejects it, override `KIND_NODE_IMAGE`.
- `make kind-down` deletes all cluster data, PVCs included; Postgres re-runs its init script on
  the next cluster.

## 3b. Policy as code (Kyverno - Phase 12.4)

Kyverno and its ClusterPolicies are installed by Argo CD from the GitOps repo B
(`platform/policies/apps/kyverno.yaml` wave -2, `kyverno-policies.yaml` wave -1,
namespaces at wave -5). Three policies are enforced (privileged containers,
mutable/`latest` tags, non-root); the rest start in audit. The `policy-validate`
CI job runs the pinned Kyverno CLI against the rendered chart and against a
deliberately insecure fixture.

Full details - enforced-vs-audited table, local commands, promoting Audit to
Enforce, and the kind/kindnet caveat - are in
[`kubecommerce-gitops/platform/policies/README.md`](../kubecommerce-gitops/platform/policies/README.md).

## 4. Deploy and roll back (Phase 5/8 - planned)

### Helm (direct)

```bash
# Render first (no cluster writes)
helm template kubecommerce kubecommerce-gitops/charts/kubecommerce \
  -f kubecommerce-gitops/environments/dev/values.yaml

# Install/upgrade the dev environment
make helm-install
# equivalent to:
# helm upgrade --install kubecommerce <chart> -n kubecommerce-dev \
#   --create-namespace -f <chart>/../environments/dev/values.yaml
```

### Argo CD (GitOps - Phase 8)

```bash
argocd app list
argocd app get kubecommerce-dev
argocd app sync kubecommerce-dev
argocd app history kubecommerce-dev
```

Promotion flow: CI publishes an immutable image SHA -> CI opens a PR that updates the
GitOps values -> merge -> Argo CD syncs dev -> smoke test -> promote the **same digest**
to staging (no rebuild) -> production PR with environment approval -> Argo CD rolling
deploy.

### Rollback

```bash
# GitOps rollback: revert the image-tag commit and push; Argo CD reconciles back.
git revert <commit> && git push

# Or pin the previous known-good digest in the GitOps values, then:
argocd app sync kubecommerce-prod

# Helm-only rollback (when not using GitOps):
helm rollout status kubecommerce -n kubecommerce-prod    # if supported
helm history kubecommerce -n kubecommerce-prod
helm rollback kubecommerce <revision> -n kubecommerce-prod
```

Rollback restores a previously published immutable digest; it never rebuilds.

### AWS EKS (Phase 13 - authored, not applied)

The cloud path lives in [`infra/terraform`](../infra/README.md) (repo A) with the cluster-side
wiring in the GitOps repo (`platform/cloud-apps`, `platform/external-secrets`,
`platform/cert-manager`). Nothing is applied automatically; `terraform apply` is a cost
decision (see the cost table in `infra/README.md`).

1. Apply an environment:
   `cd infra/terraform/environments/dev && terraform init && terraform plan && terraform apply`.
2. Wire the outputs into the GitOps repo: set the chart image registry to `ecr_registry`,
   replace `<aws-region>` in the `ClusterSecretStore`, and align the secret paths with
   `secrets_path_prefix`.
3. Register the cluster with Argo CD (`aws eks update-kubeconfig --name <cluster>`, then
   `argocd cluster add`), and flip the cloud Applications (`platform/cloud-apps`) from manual
   sync to automated once the CRDs are Established.
4. Secrets flow: AWS Secrets Manager (`/<env>/kubecommerce/...`) -> External Secrets Operator
   -> `<service>-secrets` in `kubecommerce-<env>`. Rotation is picked up on the
   `refreshInterval`; restart deployments to reload (`kubectl rollout restart deploy`).
5. TLS/DNS: point the domain at the Gateway LoadBalancer, uncomment the HTTPS listener
   hostname + `cert-manager.io/cluster-issuer` annotation and the HTTPRoute hostnames, then
   verify `Certificate`/`Gateway` readiness (see `platform/cert-manager/README.md`).
6. Teardown when not demoing: `terraform destroy` plus any lingering load balancers, NAT
   gateways and RDS snapshots; the `budget` module alerts on spend.

AWS image promotion uses the same GitOps flow as local: CI publishes to ECR through the OIDC
role (`release.yml` dispatch input `registry: ecr`) and updates GitOps values; production stays
a reviewed PR.

## 4b. CI/CD pipeline (Phase 7)

Workflows live in `.github/workflows/`:

| File | Triggers | Notes |
|---|---|---|
| `ci.yml` | pull requests + push to `main` | required checks; `permissions: contents: read` by default |
| `release.yml` | push `main`, `v*` tags, manual | GHCR push, scan, SBOM, provenance, optional signing, GitOps PR |
| `security.yml` | weekly schedule + manual | full-history CodeQL, Trivy SARIF, OpenSSF Scorecard |

**Required checks (branch protection).** Require exactly these in `ci.yml`:
`changes`, `ci-required`, `dependency-security` (PRs only), `code-security`,
`container-config-security`, `compose-validation`. **Do not require `lint`,
`typecheck`, `unit-test` or `integration-test`**: they are dynamic matrix jobs
(leg names vary per changed service) and may be conditionally skipped, which branch
protection cannot require reliably. `ci-required` is the stable aggregate gate - it
`if: always()` `needs` every CI job and fails if any dependency ended `failure` or
`cancelled`. Jobs that are *skipped* report `skipped`, which counts as passing, so
`ci-required` is the only real gate. The `changes` job decides what runs and the
force-all paths (tests, compose, scripts, Makefile, workflow files) guarantee a
PR touching the integration suite still runs every gate.

### Release flow

1. Merge to `main` (all required checks green).
2. `release.yml` detects the changed services, then builds the image **locally**
   (`load: true`, no push, `type=gha` cache) so Trivy can scan it **before** anything
   is published; only after the CRITICAL gate passes is the same image re-pushed with
   BuildKit provenance. Tags: `ghcr.io/<lowercased-owner>/kubecommerce-<service>:<full-git-sha>`
   (plus `:vX.Y.Z` on tags). `latest` is never pushed.
3. Trivy scans the local image before push; CRITICAL findings fail the release and
   nothing is published. A manual `registry: ecr` dispatch skips all GHCR build/scan/
   SBOM/attest/sign steps and only exercises the AWS OIDC placeholder job.
4. SPDX + CycloneDX SBOMs are uploaded as artifacts (and attached to the GitHub
   Release on tags). Each image gets a build provenance attestation and, when
   enabled (tag or manual `sign`), a keyless Cosign signature.
5. `gitops-promotion` updates the changed services' tag in the GitOps repository
   `environments/dev/values.yaml` and opens a PR. Merge it and Argo CD reconciles dev.
6. **Production** is a separate reviewed PR against `environments/prod/values.yaml`
   with environment approval - CI never auto-approves or deploys production.

Rollback: `git revert` the GitOps promotion commit (Argo CD self-heals), or pin the
previous digest in the values file. Images are immutable, so no rebuild is needed.

### Configuration (GitHub settings)

| Name | Kind | Purpose |
|---|---|---|
| `GITOPS_TOKEN` | secret | PAT/GitHub App token with `contents: write` + `pull_requests: write` on the GitOps repo. Rotate like any credential (section 7). |
| `GITOPS_REPO` | variable (optional) | GitOps repository name, default `kubecommerce-gitops` |
| `AWS_ROLE_ARN` | variable (optional) | IAM role assumed via GitHub OIDC for the ECR placeholder |
| `AWS_REGION` | variable (optional) | Default `us-east-1` |
| `AWS_ACCOUNT_ID` | variable (optional) | ECR account for `amazon-ecr-login` |

`release.yml` checks out the **GitOps repository** (repo B) with `GITOPS_TOKEN` into
`gitops/`; the application repo itself is never mutated by CI except for uploaded
artifacts.

### Supply-chain controls

- Third-party Actions are pinned to full commit SHAs with a `# vX.Y.Z` comment;
  Dependabot (`.github/dependabot.yml`) updates them weekly.
- Docker layers are cached with `type=gha` (per-service scope); uv is cached by
  `astral-sh/setup-uv`.
- Images are referenced by SHA/digest, never by mutable tag.
- AWS authentication uses OIDC federation only - no static `AWS_ACCESS_KEY_ID`.

### Local equivalents

```bash
make lint && make typecheck && make test
make integration-test                      # compose up + tests/integration
~/.local/bin/actionlint -color .github/workflows/*.yml
python3 -c "import yaml,glob;[list(yaml.safe_load_all(open(f))) for f in glob.glob('.github/**/*.yml',recursive=True)]"
```

## 5. Database migrations

Schema changes are versioned with Alembic and run as an **explicit Job/release step**,
never opportunistically by every replica on startup.

```bash
# From each DB-owning service directory
cd services/auth-service
uv run alembic revision --autogenerate -m "add users table"
uv run alembic upgrade head

# In Kubernetes (Phase 4/5): a dedicated migration Job per environment
kubectl -n kubecommerce-dev get jobs
kubectl -n kubecommerce-dev logs job/<service>-migrate
```

Rules:

- Migrations must be **backward compatible** for rolling deployments
  (add columns/tables first, drop later in a subsequent release).
- The migration Job runs before the new Deployment becomes ready.
- Never let application startup run migrations implicitly.

## 6. Debugging guides

### 6.1 CrashLoopBackOff

```bash
kubectl -n <ns> get pod <pod>
kubectl -n <ns> describe pod <pod>            # look at Last State, Events, exit code
kubectl -n <ns> logs <pod> --previous         # the crashed container's logs
kubectl -n <ns> logs <pod> -c <container>
```

Common causes: bad config/env var, missing secret key, import error, wrong port,
readiness/liveness command that always fails, read-only root filesystem writing to a
path that needs an `emptyDir`. Fix the config, then `kubectl rollout restart deploy/<name> -n <ns>`.

### 6.2 ImagePullBackOff / ErrImagePull

```bash
kubectl -n <ns> describe pod <pod>            # exact registry/auth error
```

Causes: image tag/digest does not exist, private registry credentials missing
(`imagePullSecrets` / registry auth), network egress blocked, or a typo in
`IMAGE_REGISTRY`. Verify the immutable SHA was actually pushed by CI.

### 6.3 Pending pods

```bash
kubectl -n <ns> describe pod <pod>            # look for "FailedScheduling"
kubectl -n <ns> get events --sort-by=.lastTimestamp
kubectl describe nodes | grep -A5 "Allocated resources"
```

Causes: insufficient CPU/memory requests, no node matching nodeSelector/affinity,
topology spread constraints too strict, unbound PVC, or taints without tolerations.

### 6.4 Running but readiness failing (no traffic)

```bash
kubectl -n <ns> describe pod <pod> | grep -A3 Readiness
kubectl -n <ns> exec -it <pod> -- wget -qO- localhost:8000/health/ready
kubectl -n <ns> get endpoints <service>       # empty endpoints == no ready pods
```

`/health/ready` failing usually means a dependency is unreachable (DB/Redis/RabbitMQ)
or the readiness probe path/port is wrong. Liveness should remain dependency-light so
a database outage does not restart every pod. Finally check the Service `targetPort`
matches the container port.

### 6.5 HPA not scaling

```bash
kubectl top pods -n <ns>                       # works only if metrics-server is healthy
kubectl -n kube-system get deploy metrics-server
kubectl -n <ns> describe hpa <name>            # Current/Desired replicas, events
kubectl get --raw "/apis/metrics.k8s.io/v1beta1/namespaces/<ns>/pods" | head
```

Causes: metrics-server missing/unhealthy, no CPU **requests** set (HPA cannot compute
utilization without requests), target already at max, or load too low to cross the
target. Missing `kubectl top` output almost always points at metrics-server.

### 6.6 NetworkPolicy blocking DNS or traffic

```bash
kubectl -n <ns> get networkpolicy
kubectl -n <ns> describe networkpolicy <name>
kubectl -n <ns> exec -it <pod> -- python3 -c "import socket;print(socket.gethostbyname('postgres'))"
kubectl -n <ns> exec -it <pod> -- python3 - <<'PY'
import socket; s=socket.create_connection(('catalog-service',8000),3); print('tcp ok'); s.close()
PY
```

Default-deny egress must still allow **DNS (UDP/TCP 53 to kube-dns/CoreDNS)** or name
resolution fails. Remember a NetworkPolicy object does nothing unless the CNI enforces
it (kind's default kindnet does not; use Calico/Cilium or rely on EKS VPC CNI + network
policy agent).

### 6.7 RabbitMQ backlog / DLQ growing

```bash
kubectl -n <ns> exec -it rabbitmq-0 -- rabbitmqctl list_queues name messages consumers
kubectl -n <ns> exec -it rabbitmq-0 -- rabbitmqctl list_queues name messages_ready messages_unacknowledged
kubectl -n <ns> logs -f deploy/notification-worker
```

If `order.created` depth grows, the consumer is down/too slow or stuck in retries.
Inspect DLQ contents; after fixing the handler, move messages back with the
shovel/policy mechanism. Watch for poison messages (always failing -> DLQ after
`WORKER_MAX_RETRIES`).

### 6.8 Redis down (fail-open behavior)

The gateway uses Redis for rate limiting and the worker uses it for dedupe.
Design intent: **rate limiting fails open** when Redis is unavailable (requests are
allowed) so a Redis outage degrades protection rather than taking down the API.
Gateway readiness is intentionally dependency-free, so a Redis outage does NOT remove the
gateway from Service endpoints. The degradation is visible through the
`dependency_up{dependency="redis"}` gauge and its transition logs.

```bash
kubectl -n <ns> get pods -l app.kubernetes.io/name=redis
kubectl -n <ns> logs deploy/redis
kubectl -n <ns> exec -it redis-0 -- redis-cli ping
```

The worker's dedupe store failing is treated conservatively: the message is rescheduled
through the retry tiers (never acked-and-dropped). Restore Redis and confirm the
`dependency_up{dependency="redis"}` gauge returns to 1.

## 6b. Observability stack (Phase 9-10)

Installed by Argo CD from the GitOps repo (`platform/observability-apps`, sync-waves -3 to 1):

| Component | Release / namespace | Local access |
|---|---|---|
| kube-prometheus-stack (Prometheus, Alertmanager, Grafana, kube-state-metrics, node-exporter) | `kube-prometheus-stack` / `monitoring` | `kubectl -n monitoring port-forward svc/kube-prometheus-stack-grafana 3000:80` (admin password in the `kube-prometheus-stack-grafana` Secret), Prometheus `:9090`, Alertmanager `:9093` |
| Loki (monolithic, filesystem) | `loki` / `observability` | `kubectl -n observability port-forward svc/loki 3100:3100` |
| Tempo (single binary, local storage) | `tempo` / `observability` | `kubectl -n observability port-forward svc/tempo 3200:3200` |
| OpenTelemetry Collector (gateway Deployment) | `otel-collector` / `observability` | OTLP HTTP `:4318` in-cluster |
| Grafana Alloy (log DaemonSet) | `alloy` / `observability` | n/a |
| alert-sink (Alertmanager webhook receiver) | `alert-sink` / `observability` | `kubectl -n observability port-forward svc/alert-sink 8080:8080`, then `curl localhost:8080/alerts` |

Grafana datasources (pinned UIDs): `prometheus` -> `http://kube-prometheus-stack-prometheus.monitoring:9090`,
`loki` -> `http://loki.observability:3100` (derived field `trace_id` links to Tempo),
`tempo` -> `http://tempo.observability:3200`. Dashboards are provisioned from ConfigMaps in
`platform/monitoring/dashboards` into the `KubeCommerce` folder.

Cross-signal debugging (guide 10.4):

1. Grafana -> KubeCommerce / API RED: identify the slow or erroring service.
2. Loki query `{namespace="kubecommerce-dev"} |= "<correlation-id>"`, then click the `trace_id`
   derived field to open the Tempo trace.
3. In Tempo use "Logs for this span" / "Traces to metrics" to pivot back to the correlated logs
   and service metrics.

Alerts: 8 PrometheusRules in `observability` (5xx rate >5%, p95 >750 ms, no successful requests,
pod restarts, unavailable replicas, memory near limit, HPA at max replicas, RabbitMQ DLQ
non-empty). Alertmanager routes by severity to the local `alert-sink` webhook with
`send_resolved: true`, so firing and resolution can be demonstrated without an external account.

Tracing: services export OTLP HTTP to `http://otel-collector.observability.svc.cluster.local:4318`
(`OTEL_ENABLED=true` in all overlays). Traces -> Tempo, logs -> Loki via Alloy; application RED
metrics stay on `/metrics` and are scraped through ServiceMonitors (Prometheus uses open
selectors, so no `release` label is required on the ServiceMonitors).

## 7. Secret rotation (Phase 6.3 - planned demo)

Goal: rotate a credential **without rebuilding the application image**.

1. Update the value in the source of truth:
   - AWS Secrets Manager path, e.g. `/prod/kubecommerce/database/auth-url`; or
   - the local generated Kubernetes Secret (never committed).
2. External Secrets Operator pulls the new value and updates the Kubernetes Secret.
3. Reload:
   - ConfigMap/Secret via projected volume or reloader annotation -> restart pods, or
     the app reads the value on startup / on next connection.
   - Document whether a restart is required per credential.
4. Verify: `kubectl -n <ns> get secret <name> -o jsonpath='{.data}'` metadata only,
   then check readiness and a smoke test.
5. Confirm no production credential is committed to GitHub.

JWT signing-key rotation (no downtime): publish the new public key in JWKS alongside the
old, switch signing to the new private key, wait for old token TTL (900 s), then remove
the old public key.

## 8. Incident triage checklist

1. **Impact** - which environment/service, user-facing vs internal, started when.
2. **Golden signals** - check Grafana API RED dashboard (rate, errors, p95 latency) and
   the Kubernetes workload dashboard (restarts, unavailable replicas, HPA).
3. **Blast radius** - one pod, one service, one node, or cluster-wide?
4. **Recent changes** - latest GitOps sync / image digest / migration Job.
5. **Logs** - Loki query by `service`, `environment`, `severity`, and `correlation_id`.
6. **Traces** - open a slow/erroring trace in Tempo; find the slow downstream span.
7. **Alerts** - check Alertmanager for firing vs silenced alerts.
8. **Mitigate** - roll back the GitOps commit / scale / fail over / disable a feature.
9. **Communicate** - state impact + mitigation + ETA.
10. **Follow up** - write the postmortem with timeline, root cause, and preventive action.

## 9. Maintenance

- **Version pinning.** Pin Python deps via each project's `uv.lock`; pin GitHub Actions
  to commit SHAs; pin container base images by digest for releases; pin Helm chart and
  CRD versions together.
- **Dependency automation.** Dependabot/Renovate opens PRs for uv, Actions, Docker, and
  Helm updates; review and validate compatibility before merging.
- **Cleanup.** `make clean` removes caches, venvs, and build artifacts. Remove old image
  tags (keep the currently deployed digest).
- **Upgrade order.** Kubernetes -> CRDs/controllers -> Helm charts -> application.
  Validate in dev, then staging, then prod (GitOps PR).
- **Maintenance rule.** Do not copy version numbers from the plan blindly months later;
  validate compatibility between Kubernetes, Helm, CRDs, and controllers.

## 10. Reliability demo runbook

`scripts/reliability-demo.sh` collects evidence for the guide Phase 11 demos and
the section 13 demo-script step 9 ("kill a pod and show self-healing"). It needs
`kubectl` and a reachable kind cluster (`make kind-up`) with the app deployed;
`k6` is additionally required for the `hpa` mode. It never deletes namespaces or
PVCs, stops its own background load, and uncordons any node it drained (EXIT trap).

Prerequisites and setup:

```bash
export PATH="$HOME/.local/bin:$PATH"
make kind-up                                  # cluster + platform
make helm-install                             # or let Argo CD sync kubecommerce-dev
kubectl -n kubecommerce-dev get deploy,hpa,pdb
scripts/reliability-demo.sh --help
```

Run one demo at a time, or all of them (each writes into its own subdirectory of
`artifacts/reliability/<UTC timestamp>/`):

```bash
scripts/reliability-demo.sh rolling
scripts/reliability-demo.sh hpa
scripts/reliability-demo.sh crash
scripts/reliability-demo.sh disruption
scripts/reliability-demo.sh rollback
scripts/reliability-demo.sh all               # writes 00-summary.md
```

Expected observations:

| Mode | Command | Expected observation |
|---|---|---|
| Rolling update | `rolling` | `kubectl rollout status` completes; `availability-summary.txt` shows `server_errors_5xx_or_connection=0` and `result=PASS`. Fails only on 5xx/connection errors; 4xx are reported separately as `client_4xx`. |
| HPA | `hpa` | k6 runs (`load.log`), `replica-trajectory.txt` shows `current` rising above the initial count; fails if no scale-up occurred. Needs an HPA-enabled namespace. |
| Crash recovery | `crash` | the deleted pod's name+UID (`deleted-pod.txt`) is gone and a **different, Ready pod** (new name+UID) replaces it (`self-healing.txt`); `kill 1` increments the **same** pod's `restartCount` (`kill1-exec.txt`). |
| Disruption | `disruption` | node is cordoned then drained (`drain.txt`), PDB state captured (`pdb-during.txt`), availability loop stays clean, node is uncordoned (`uncordon.txt`). |
| Rollback | `rollback` | a new revision becomes Ready, `rollout undo` returns the previous one, availability stays clean. |

Configuration (environment variables): `NAMESPACE` (default `kubecommerce-dev`),
`RELEASE` (`kubecommerce`), `BASE_URL` (`http://localhost/`), `TARGET_DEPLOY`
(`gateway-api`), `HPA_NAME` (default: first HPA in the namespace),
`K6_VUS`/`K6_DURATION`, `ARTIFACTS_DIR`, `HPA_WATCH_SECONDS`, `ROLLOUT_TIMEOUT`,
`POD_READY_TIMEOUT`, `DRAIN_TIMEOUT`, `NODE_NAME`, and `FORCE`.

Cleanup:

- The script restores cluster state itself; verify with
  `kubectl get nodes` (no `SchedulingDisabled`) and
  `kubectl -n kubecommerce-dev get pods`.
- Background load is stopped in the EXIT trap; if a run was interrupted, check
  `pgrep -af order-flow.js` and `kill` any leftover k6.
- Evidence is kept under `artifacts/reliability/`; the directory is git-ignored
  and safe to delete with `make clean`.

Safety notes:

- Any `kubecommerce-prod*` namespace is refused unless `FORCE=1` is set.
- `disruption` evicts every pod on one worker node. A PDB that forbids eviction
  makes `kubectl drain` itself fail, so the script reports the **drain step** as
  failed (`drain_ok=0`); a single-replica dev deployment can instead cause real
  request failures while pods reschedule, which fails the **availability
  assertion** (`availability_ok=0`). Prefer staging/prod values (3 replicas, PDB
  `minAvailable: 2`, topology spread) for a clean disruption demo.
- Never run `disruption` on a cluster whose only worker also hosts state you
  cannot lose; Postgres keeps its PVC, but pods are evicted and rescheduled.

