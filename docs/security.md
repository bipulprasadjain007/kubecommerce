# KubeCommerce - Security

This document describes the security model, what is implemented today versus planned,
and the operational procedures around secrets, supply chain, and Kubernetes hardening.

> **Honesty rule:** controls are marked **Status: implemented** only when they exist in
> this repository at the time of writing. Everything else is **Status: planned** with the
> phase that delivers it. Do not present planned controls as working controls.

## 1. Threat model summary

| Threat | Example | Primary mitigations |
|---|---|---|
| Credential theft from source | JWT private key, DB password committed to Git | keys generated outside source (`.secrets/`, git-ignored); `.env.example` only placeholders; `detect-private-key` pre-commit hook; secret scanning; Secrets Manager in cloud |
| Token forgery / replay | forged `X-User-ID`, stolen bearer token | RS256 signed tokens, gateway verifies via JWKS (`iss`/`aud`/`exp`); short TTL (900 s); internal services not publicly reachable; `X-Internal-Token` for internal mutations |
| Lateral movement in cluster | compromised pod calls other services/DBs | default-deny NetworkPolicy (planned), non-root, dropped capabilities, least-privilege RBAC, dedicated ServiceAccounts |
| Malicious/compromised image | vulnerable or backdoored dependency | Trivy scans, SBOM, provenance, Cosign keyless signature, immutable digest deploy, no `latest` in prod |
| CI credential abuse | static AWS keys in GitHub | GitHub Actions OIDC -> short-lived AWS IAM role; least-privilege workflow permissions; Actions pinned to SHAs |
| Data exposure via logs | passwords/tokens written to logs | logging hygiene rules; secret redaction; no credentials in error `details` |
| Resource abuse | unauthenticated flood, runaway pod | gateway rate limiting (Redis), resource requests/limits, HPA, PDB |
| Insider/GitOps tampering | silent prod change | prod changes via reviewed GitOps PR + environment approval; Argo CD reconciliation; branch protection |

## 2. Secrets management

### Local (implemented)

- `scripts/dev-bootstrap.sh` generates an RSA-2048 JWT keypair into `.secrets/`
  (`chmod 700` dir, `chmod 600` private key) only if missing, and creates `.env` from
  `.env.example` without overwriting.
- `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), and `.secrets/`.
- Pre-commit `detect-private-key` blocks accidental private-key commits.
- No real secret value is stored in `.env.example` or any tracked file.

### Kubernetes / AWS (Status: planned - Phase 6 / Phase 13)

- Local cluster secrets may use SOPS + age or generated Kubernetes Secrets excluded from
  Git. Never commit plaintext production secrets to Helm values.
- AWS: **AWS Secrets Manager** as source of truth, surfaced into the cluster by the
  **External Secrets Operator**. Example paths:

```text
/prod/kubecommerce/database/auth-url
/prod/kubecommerce/database/catalog-url
/prod/kubecommerce/database/orders-url
/prod/kubecommerce/auth/jwt-signing-key
/prod/kubecommerce/internal/api-token
# ... the full 10-path list lives in infra/README.md section 6
```

- Workloads use **EKS Pod Identity** (short-lived, per-workload IAM roles), not static
  AWS access keys embedded in Kubernetes Secrets.
- Rotation procedure: see `docs/runbook.md` section 7. Rotation must not require
  rebuilding the application image.

## 3. GitHub Actions OIDC (Status: implemented - Phase 7)

- Workflows authenticate to AWS via **GitHub OIDC federation** to a narrowly scoped IAM
  role; no long-lived `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` repository secrets.
  Implemented in `.github/workflows/release.yml` as the manual, disabled-by-default
  `aws-oidc-placeholder` job (`registry: ecr`, `id-token: write`, role assumed from
  `vars.AWS_ROLE_ARN`).
- Workflow `permissions:` are least-privilege (default read-only; grant `id-token: write`
  only where OIDC is needed, `packages: write` only for publishing, `security-events: write`
  only for CodeQL/Trivy/SARIF uploads).
- Third-party Actions are pinned to immutable commit SHAs with a `# vX.Y.Z` comment on
  every pipeline; Dependabot (`.github/dependabot.yml`) keeps the SHA and comment current.
- Repository security: branch protection with required status checks, required reviews,
  CODEOWNERS, secret scanning, and Dependabot. These GitHub-side settings are configured
  **manually** (they cannot be created from this machine) - see README
  "GitHub repository setup" for the exact required check names and secret/variable list.

## 4. Software supply chain (Status: implemented - Phase 7)

Pipeline controls, implemented in `.github/workflows/`:

- **Immutable image identity:** every release is tagged with the full Git SHA
  (`ghcr.io/<owner>/kubecommerce-<service>:<sha>`, plus `:vX.Y.Z` on version tags) and
  referenced by digest downstream; a mutable `latest-dev` exists locally only;
  production never deploys `latest`. `latest` is never pushed by CI.
- **Vulnerability scanning:** Trivy scans the filesystem, secrets and misconfigurations
  on every push/PR (`ci.yml`), and each pushed image by digest during release
  (`release.yml`). The release gate blocks known **CRITICAL** findings.
- **SBOM:** generated per image as **SPDX and CycloneDX** (`anchore/sbom-action`),
  uploaded as artifacts and attached to the GitHub Release on tags.
- **Build provenance:** BuildKit `provenance: true` plus
  `actions/attest-build-provenance` for each image digest.
- **Signing:** optional **Cosign keyless** signing via OIDC (enabled on `v*` tags or the
  manual `sign` input).
- **Dependency review** on pull requests (`actions/dependency-review-action`).
- **Scheduled scanning:** `security.yml` runs full-history CodeQL, Trivy filesystem SARIF
  and OpenSSF Scorecard weekly.
- **GitOps promotion** opens a pull request for `environments/dev/values.yaml`; production
  remains a reviewed PR with environment approval (never auto-approved).

### Documented exception path

Current accepted infrastructure exceptions (each justified inline in `.trivyignore`):
`AWS-0039` (EKS secrets encryption not enabled in the authored module), `AWS-0040`/`AWS-0041`
(public, CIDR-restricted EKS API endpoint needed for GitOps access) and `AWS-0104` (EKS-managed
cluster security group egress). These apply only to the author-only Terraform path and must be
re-reviewed before any real deployment.

A release gate may be waived only by a **narrow, documented exception**:

1. Record the finding (CVE/rule id), the affected artifact, the reason it is not
   exploitable in this context, and an expiry/review date.
2. Add a minimal `.trivyignore` entry (`CVE-...` or the misconfig rule id) at the repo
   root, referenced by the Trivy steps, and link the tracking issue in the PR.
3. Re-evaluate before the expiry date; remove the entry once fixed or upgraded. Blanket
   or unbounded suppressions are not acceptable.

Local equivalent: `make security-scan` runs `trivy fs` and `trivy config` **if trivy is
installed**; otherwise it prints a clear skip message. Trivy is not installed in this
working copy, so local results are unverified; the CI runs are authoritative.

## 5. Kubernetes baseline (Status: planned - Phase 4/12)

Target security context per container:

```yaml
securityContext:
  runAsNonRoot: true
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  capabilities:
    drop: ["ALL"]
  seccompProfile:
    type: RuntimeDefault
```

Additional controls:

- **Dedicated ServiceAccounts** per service; `automountServiceAccountToken: false` where
  no Kubernetes API access is needed.
- **RBAC least privilege** - no broad `cluster-admin` bindings for workloads.
- **NetworkPolicy default-deny** ingress/egress with explicit allows (DNS, service
  dependencies, metrics scraping). Enforced only if the CNI implements policies.
- **Pod Security Admission** namespace labels (`restricted`/`baseline`) appropriate to
  each namespace.
- Writable paths only where genuinely needed, via `emptyDir` (root filesystem read-only).

## 6. Policy as code (Status: implemented - Phase 12.4)

Kyverno runs in the local/GitOps cluster (Helm chart pinned in repo B,
`platform/policies/`) and applies cluster-scoped `ClusterPolicy` resources that
encode the Kubernetes hardening requirements. Policies start **audit-heavy**; the
three policies the hardened chart already satisfies are **enforced** from day one.

- disallow privileged containers / privilege escalation (**Enforce**);
- disallow `latest` and empty image tags (**Enforce**);
- require non-root execution (**Enforce**);
- require resource requests/limits (Audit);
- require readiness **and** liveness probes (Audit, Deployments);
- require approved image registries - `ghcr.io/bipulprasadjain007/*`, with an
  explicit exception list for infra namespaces (Audit);
- require `seccompProfile: RuntimeDefault` (Audit);
- require `readOnlyRootFilesystem: true` (Audit, kubecommerce namespaces);
- require `capabilities.drop: ["ALL"]` (Audit);
- disallow hostPath volumes and host namespace access
  (`hostNetwork`/`hostPID`/`hostIPC`) (Audit).

Container checks cover `containers`, `initContainers` and (where the API allows
it) `ephemeralContainers`. Policies are scoped to the `kubecommerce-*`
namespaces (plus `platform-system` where it makes sense); the label-selector
alternative is **namespace-bounded**, so a pod carrying
`app.kubernetes.io/part-of: kubecommerce` in another namespace (for example the
OTel collector in `observability`, which needs the label as a NetworkPolicy
peer) is not matched or denied. Kubernetes system namespaces are never matched.

Fixtures in `platform/policies/tests/` pin both directions: a deliberately
insecure manifest (`insecure-deployment.yaml`) and a hardened golden-path
manifest (`compliant-workload.yaml`).

The repo B `policy-validate` CI job downloads the SHA-256-verified pinned Kyverno
CLI and asserts four things: the namespace-stamped Helm-rendered dev chart
produces no violations, the insecure fixture produces exactly the expected
violation count with every policy name present (so deleting a policy cannot
pass), the compliant fixture passes, and the hardened OTel collector stays
outside the policy scope. This is the machine-checkable half of the exit
criterion ("a deliberately insecure test manifest is rejected by CI and/or
cluster policy"). Promoting an Audit policy to Enforce is a one-line
`validationFailureAction` change after remediation; see
`platform/policies/README.md` in the GitOps repository.

## 7. Logging hygiene (Status: implemented by design; enforced during Phase 1)

- Never log passwords, bearer tokens, JWT private keys, full card-like data, or raw
  credentials.
- Structured JSON logs include safe fields only: timestamp, severity, service,
  environment, correlation ID, trace ID, request method/path **template**, status,
  duration, and a safe error category.
- Error responses use the consistent schema
  `{"error":{"code","message","correlation_id","details"?}}`; `details` must not contain
  secrets.
- Avoid unbounded metric labels (raw user IDs, URLs containing IDs) to prevent
  high-cardinality leakage and cardinality blowups.

## 8. Image and dependency patching

- Base images pinned by digest for release builds; rebuild regularly to pick up base
  image security updates.
- Dependabot/Renovate PRs for Python (uv), Actions, Docker, and Helm dependencies.
- Trivy image scan re-run on every build; Critical findings block release unless
  documented.
- Kubernetes/Helm/CRD/controller versions validated together before upgrades
  (see runbook maintenance section).

## 9. Status summary

| Control | Status |
|---|---|
| Local key generation into `.secrets/`, git-ignored | implemented (script) |
| `.env.example` placeholders only; `.env` git-ignored | implemented |
| `detect-private-key` pre-commit hook | implemented |
| RS256 JWT + JWKS verification | implemented (Phase 1/2) |
| Gateway-only public entry, `X-User-ID`, `X-Internal-Token` | implemented (Phase 1/2) |
| AWS Secrets Manager + External Secrets Operator | planned (Phase 6/13) |
| EKS Pod Identity | planned (Phase 13) |
| GitHub Actions OIDC, least-privilege permissions, SHA-pinned Actions | implemented (Phase 7: `ci.yml`, `release.yml`, `security.yml`) |
| Trivy scans / SBOM / provenance / Cosign keyless | implemented (Phase 7: release + security workflows) |
| Dependency review on PRs, CodeQL, weekly Scorecard | implemented (Phase 7/12) |
| Dependabot for uv / Actions / Docker | implemented (`.github/dependabot.yml`, grouped weekly) |
| non-root, read-only rootfs, drop ALL, seccomp, PSA | planned (Phase 4/12) |
| NetworkPolicy default-deny | planned (Phase 4/12) |
| Kyverno policy as code: Enforce (privileged/escalation, mutable tags, non-root) + Audit (resources, probes, registries, seccomp, read-only rootfs, drop ALL, hostPath/hostNetwork/hostPID/hostIPC); namespace-bounded, covers init/ephemeral containers | implemented (Phase 12.4, GitOps repo B `platform/policies/`); `policy-validate` CI gate (chart + insecure + compliant + OTel) |
| Branch protection, required reviews, CODEOWNERS, secret scanning (GitHub settings) | manual GitHub setup (documented in README/runbook) |
