# KubeCommerce - AWS infrastructure (Terraform)

> **Nothing in this directory has been applied.** No AWS account was touched
> while authoring it: `terraform apply` (and therefore every hourly charge
> below) is **your explicit cost decision**. The code has only been
> statically validated (`terraform fmt`, `terraform validate`,
> `actionlint`) with no credentials and no cluster.

Terraform for the Phase 13 AWS path: VPC, EKS, ECR, an optional RDS
PostgreSQL, Secrets Manager, IAM roles for GitHub OIDC / EKS Pod Identity,
and an AWS Budget guardrail. It is deliberately small and cost-conscious so a
portfolio environment can be stood up and torn down again.

---

## 1. Layout

```
infra/terraform/
  modules/
    network/              VPC, 3 AZs, public+private subnets, single NAT (default)
    eks/                  EKS control plane, managed node group, OIDC provider, addons
    ecr/                  5 immutable, scan-on-push repositories + lifecycle policy
    rds/                  optional PostgreSQL 16 (flag-gated by the environment)
    secrets/              Secrets Manager paths /<env>/kubecommerce/*
    iam/github_oidc/      GitHub OIDC provider + least-privilege CI role
    iam/pod_identity/     ESO / AWS Load Balancer Controller / optional app S3 roles
    budget/               AWS Budget with 50/80/100% alerts
  environments/
    dev/                  RDS disabled; single NAT; small node group
    prod/                 RDS enabled (Multi-AZ, deletion protection)
```

Each environment is an independent root module with its own state.

| Module | Key inputs | Key outputs |
|---|---|---|
| `network` | `vpc_cidr`, `az_count`, `single_nat_gateway` | `vpc_id`, `private_subnet_ids`, `public_subnet_ids` |
| `eks` | `subnet_ids`, `cluster_version`, `node_instance_types`, `public_access_cidrs` | `cluster_name`, `cluster_endpoint`, `oidc_provider_arn`, `kubeconfig_command` |
| `ecr` | `name_prefix`, `repository_names` | `repository_urls`, `repository_arns`, `registry_id` |
| `rds` | `vpc_id`, `subnet_ids`, `allowed_security_group_ids` | `address`, `port`, `master_user_secret_arn` |
| `secrets` | `name_prefix`, `secret_paths` | `secret_arns`, `path_prefix` |
| `iam/github_oidc` | `ecr_repository_arns`, `allowed_subjects`, `create_oidc_provider`, `existing_oidc_provider_arn` | `role_arn`, `oidc_provider_arn` |
| `iam/pod_identity` | `cluster_name`, `secret_arns` | `external_secrets_role_arn`, `alb_controller_role_arn` |
| `budget` | `monthly_budget_usd`, `alert_emails`, `cost_filter_tags` | `budget_name` |

Two account-global resources need care when more than one environment is
applied:

- **EKS public API endpoint.** dev defaults `public_access_cidrs = ["0.0.0.0/0"]`
  for a short-lived cluster; prod defaults to the documentation placeholder
  `["203.0.113.0/24"]` and must be replaced with the operator CIDR in
  `terraform.tfvars`.
- **GitHub OIDC provider.** Apply one environment first, or reuse the
  existing provider (`create_oidc_provider = false` +
  `existing_oidc_provider_arn`). The prod root auto-reuses when the ARN is set.
- **Budget** is filtered by `cost_filter_tags = { project = "kubecommerce" }`
  so it tracks this project rather than the whole account.

---

## 2. Prerequisites

- **Terraform** `>= 1.9.0, < 2.0.0` (validated with **1.16.4**).
- **AWS provider** `~> 6.0` (validated with **6.66.0**). Downloaded by
  `terraform init`; no credentials are needed for `-backend=false`.
- **AWS CLI v2** for the backend bootstrap and `aws eks update-kubeconfig`.
- AWS credentials with permission to create the resources (for a first run,
  an admin principal; scope down afterwards).
- GitHub repository variables: `AWS_ROLE_ARN`, `AWS_REGION` (and existing
  `GITOPS_REPO`). A protected GitHub **environment named `aws`** for applies.

No secret needs to live in this repository. Secrets Manager values are
created out of band (console/CLI/CI), never in `terraform.tfvars`.

---

## 3. Bootstrap the remote backend (once per account)

State starts local; switch to S3 + DynamoDB locking before sharing or
applying from CI.

```bash
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
REGION="us-east-1"
BUCKET="kubecommerce-tfstate-${ACCOUNT_ID}"
TABLE="kubecommerce-tfstate-lock"

# 1. Versioned, encrypted, private state bucket.
aws s3api create-bucket --bucket "${BUCKET}" --region "${REGION}"
aws s3api put-bucket-versioning --bucket "${BUCKET}" \
  --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "${BUCKET}" \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
aws s3api put-public-access-block --bucket "${BUCKET}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true

# 2. Lock table (pay-per-request: no idle cost).
aws dynamodb create-table --table-name "${TABLE}" \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH \
  --billing-mode PAY_PER_REQUEST --region "${REGION}"
```

Then uncomment the `backend "s3"` block in
`environments/dev/versions.tf` and `environments/prod/versions.tf`, fill in
the bucket/table names, and re-initialise:

```bash
cd infra/terraform/environments/dev
terraform init -migrate-state
```

`terraform.tfvars.example` is the committed template. Copy it to
`terraform.tfvars` (git-ignored) and edit. Never commit `terraform.tfvars`,
state files, or secret values.

Recommended repo A `.gitignore` additions (owned by the orchestrator, not this
lane): `infra/terraform/**/.terraform/`, `*.tfstate`, `*.tfstate.*`,
`*.tfvars` (but keep `*.tfvars.example`), `crash.log`. Commit
`.terraform.lock.hcl`.

---

## 4. Cost warning table

The following are **indicative us-east-1 on-demand** figures for an *idle*
deployment and are **not** a quote - check the AWS pricing pages before
applying. EKS and NAT bill hourly whether or not you use them, which is why
the teardown section matters.

| Resource | Approx. cost | Notes |
|---|---|---|
| EKS control plane | ~$0.10/hr (~$73/mo) | Per cluster, always on |
| NAT gateway (single) | ~$0.045/hr (~$32/mo) + ~$0.045/GB | `single_nat_gateway = true` avoids 3x |
| Worker nodes, 2 x t3.medium | ~$0.042/hr each (~$60/mo) | Managed node group |
| RDS `db.t4g.micro` | ~$0.016/hr (~$12/mo) | Multi-AZ roughly doubles it; prod on |
| AWS Load Balancer (ALB) | ~$0.0225/hr (~$16/mo) + LCU | Created by the in-cluster controller, not Terraform |
| ECR storage | ~$0.10/GB/mo | Lifecycle policy caps it |
| Secrets Manager | ~$0.40/secret/mo | 10 paths + RDS-managed secret |
| CloudWatch Logs (EKS/flow logs) | variable | Control-plane logs on; flow logs off by default |
| **Indicative dev floor** | **~$200+/mo** | EKS + NAT + 2 nodes + ECR + secrets |

The `budget` module alerts at 50/80/100% of `monthly_budget_usd` (and 100%
forecasted). Set `budget_alert_emails` before the first apply.

---

## 5. Apply and destroy order

Terraform infers dependencies from module outputs, so a single
`terraform apply` per environment is enough. Conceptually:

**Apply**

1. `network` (VPC, subnets, IGW, NAT)
2. `ecr` and `secrets`
3. `eks` (control plane, node group, OIDC provider, addons)
4. `iam/github_oidc` and `iam/pod_identity` (need cluster + repo/secret ARNs)
5. `rds` (when `enable_rds = true`; needs the VPC, subnets and cluster SG)
6. `budget`

```bash
cd infra/terraform/environments/dev
terraform init
terraform plan  -out=tfplan
terraform apply tfplan

# Write a kubeconfig entry for the new cluster:
$(terraform output -raw eks_kubeconfig_command)
```

**Destroy** (order matters for resources AWS creates outside Terraform):

1. Delete Kubernetes `LoadBalancer`/`Ingress` objects first so the AWS Load
   Balancer Controller removes the ELBs. Terraform will fail to delete the
   VPC while an ELB still lives in its subnets.
2. If RDS was created: set `deletion_protection = false` and
   `skip_final_snapshot = true` (or accept the final snapshot), apply, then
   destroy.
3. `terraform destroy`.

```bash
cd infra/terraform/environments/dev
terraform destroy
```

The NAT gateway, EKS control plane and RDS instance are the expensive items;
confirm they are gone in the console after `destroy`.

---

## 6. Wiring the outputs into the GitOps repository

After apply, `terraform output` (or `-json`) gives everything the GitOps repo
and Argo CD need.

| Output | Used for |
|---|---|
| `ecr_registry` | Image registry host, e.g. `<acct>.dkr.ecr.us-east-1.amazonaws.com` |
| `ecr_repository_urls` | Per-service repository URL in `environments/<env>/values.yaml` |
| `eks_cluster_name` / `eks_kubeconfig_command` | Argo CD destination cluster / `aws eks update-kubeconfig` |
| `eks_oidc_provider_arn` | Only needed for the IRSA alternative to Pod Identity |
| `secrets_path_prefix` | ESO `SecretStore` path prefix (`/<env>/kubecommerce`) |
| `secret_arns` | Confirm the ESO read policy covers every path |
| `external_secrets_role_arn` | Pod Identity role annotated/associated with the ESO service account |
| `github_oidc_role_arn` | Repository variable `AWS_ROLE_ARN` |
| `rds_address`, `rds_port`, `rds_master_secret_arn` | Database DSNs / RDS-managed master secret |

**ECR.** Point the GitOps values at ECR (the repository names embed
`kubecommerce-<env>/<service>`):

```yaml
image:
  registry: "<ecr_registry>"
  repository: "kubecommerce-dev/auth-service"   # per service
```

The release workflow's `registry: ecr` path uses `vars.AWS_ROLE_ARN`; the
role's inline policy is scoped to exactly these repositories.

**Argo CD registration.** Register the cluster with Argo CD and update the
ApplicationSet destination:

```bash
aws eks update-kubeconfig --name "$(terraform output -raw eks_cluster_name)" --region us-east-1
argocd cluster add "$(kubectl config current-context)" --name kubecommerce-dev
```

The GitOps repo name is the existing `GITOPS_REPO` variable
(`kubecommerce-gitops`); no Terraform change is needed for promotion - the
release workflow already opens the dev promotion PR.

**External Secrets Operator.** The ESO Pod Identity role may read
`/<env>/kubecommerce/*` plus the RDS-managed secret. A `SecretStore` using
that role:

```yaml
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: aws-secretsmanager
  namespace: kubecommerce-dev
spec:
  provider:
    aws:
      service: SecretsManager
      region: us-east-1
```

The `ExternalSecret` paths then line up with the module (10 secrets):

```
/<env>/kubecommerce/database/auth-url
/<env>/kubecommerce/database/catalog-url
/<env>/kubecommerce/database/orders-url
/<env>/kubecommerce/auth/jwt-signing-key
/<env>/kubecommerce/redis/catalog-url
/<env>/kubecommerce/redis/worker-url
/<env>/kubecommerce/redis/gateway-url
/<env>/kubecommerce/rabbitmq/orders-url
/<env>/kubecommerce/rabbitmq/worker-url
/<env>/kubecommerce/internal/api-token
```

Set the real values out of band, e.g.:

```bash
aws secretsmanager put-secret-value \
  --secret-id /dev/kubecommerce/database/auth-url \
  --secret-string 'postgresql+asyncpg://kubecommerce:<pw>@<rds_host>:5432/auth_db'
```

The `secrets` module writes a `REPLACE_ME` placeholder once and then ignores
the value, so Terraform never overwrites a real secret.

---

## 7. IAM / identity design

### GitHub Actions OIDC (`modules/iam/github_oidc`)

- Creates the GitHub OIDC provider. The provider is **account-global**, so
  only one environment may create it: apply one environment first, or set
  `create_oidc_provider = false` and `existing_oidc_provider_arn` for the
  others. The prod root force-reuses an existing provider whenever
  `existing_oidc_provider_arn` is non-empty.
- One role whose trust policy only allows `sts:AssumeRoleWithWebIdentity`
  when `aud = sts.amazonaws.com` **and** the subject matches:
  - `repo:bipulprasadjain007/kubecommerce:ref:refs/heads/main` (plan job)
  - `repo:bipulprasadjain007/kubecommerce:ref:refs/tags/v*`
  - `repo:bipulprasadjain007/kubecommerce:environment:aws` (apply job, which
    is gated by the protected `aws` environment)
  - No `pull_request`/wildcard subjects are allowed.
- Default inline permission is **ECR push/pull only**, scoped to the created
  repositories. `ecr:GetAuthorizationToken` and `ecr:DescribeRepositories`
  are account-level actions that cannot be resource-scoped, so they use
  `Resource: "*"`. Terraform `plan`/`apply` needs more: attach a scoped
  deployment policy via `terraform_role_policy_arns` (opt-in). The same role
  ARN is exposed as `github_oidc_role_arn` for `vars.AWS_ROLE_ARN`.

The GitHub OIDC provider's thumbprint is intentionally left empty (AWS
provider v6 treats `thumbprint_list` as Optional+Computed) so AWS manages the
CA; pin `thumbprints` only if required.

### EKS Pod Identity (`modules/iam/pod_identity`)

EKS Pod Identity is the primary mechanism (idempotent, no per-cluster OIDC
plumbing). The `eks-pod-identity-agent` addon is installed by `modules/eks`.

| Role | Permissions | Association |
|---|---|---|
| External Secrets Operator | `secretsmanager:GetSecretValue/DescribeSecret/ListSecretVersionIds` on the KubeCommerce secret ARNs (+ `kms:Decrypt` when `kms_key_arns` is set) | `external-secrets/external-secrets` |
| AWS Load Balancer Controller | Default AWS managed `ElasticLoadBalancingFullAccess`; replace with the scoped upstream `AWSLoadBalancerControllerIAMPolicy` JSON via `alb_controller_policy_arns` | `platform-system/aws-load-balancer-controller` (the GitOps repo installs the controller there) |
| App S3 read (optional) | `s3:GetObject`, `s3:ListBucket` on one bucket | `<app_s3_namespace>/<app_s3_service_account>` - the module creates the association so the role is actually assumable |

Trust policy is `pods.eks.amazonaws.com` with `sts:AssumeRole` +
`sts:TagSession`. Every role that needs to be assumed has a matching
`aws_eks_pod_identity_association`; the External Secrets policy derives its
resource list directly from the `secrets` module output (no hardcoded ARNs).

**IRSA alternative:** remove the `aws_eks_pod_identity_association`
resources and annotate the service accounts with
`eks.amazonaws.com/role-arn: <role_arn>`. IRSA requires the OIDC provider
trust conditions (sub/aud) rather than the Pod Identity trust - adjust the
assume-role policy accordingly.

---

## 8. CI workflow

`.github/workflows/terraform.yml`:

- **fmt** - `terraform fmt -recursive -check` on PRs and pushes.
- **validate (dev|prod)** - matrix `terraform init -backend=false` +
  `terraform validate`. No credentials.
- **plan** - manual (`workflow_dispatch`), OIDC via
  `aws-actions/configure-aws-credentials` using `vars.AWS_ROLE_ARN` /
  `vars.AWS_REGION`; uploads the plan artifact. It carries **no** GitHub
  environment, so STS sees the ref subject
  `repo:bipulprasadjain007/kubecommerce:ref:refs/heads/main`.
- **apply** - manual, `needs: plan`, gated behind the protected GitHub
  environment **`aws`**; STS sees
  `repo:bipulprasadjain007/kubecommerce:environment:aws`. Applies the
  reviewed plan artifact.

All third-party actions are pinned to full commit SHAs with `# vX.Y.Z`
comments. The workflow is `actionlint`-clean.

---

## 9. Local static validation (no AWS, no cluster)

```bash
export PATH="$HOME/.local/bin:$PATH"

terraform fmt -recursive -check infra/terraform

cd infra/terraform/environments/dev
terraform init -backend=false -input=false && terraform validate

cd ../prod
terraform init -backend=false -input=false && terraform validate
```

`terraform apply` is intentionally **not** run anywhere in this repository's
automation without a human choosing the protected `aws` environment and
triggering it.
