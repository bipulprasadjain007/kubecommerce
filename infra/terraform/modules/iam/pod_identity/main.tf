# =============================================================================
# iam/pod_identity - EKS Pod Identity roles for cluster controllers.
#
# EKS Pod Identity lets a Kubernetes service account assume an IAM role without
# an OIDC/IRSA annotation: the pod identity agent (installed as the
# `eks-pod-identity-agent` addon in modules/eks) injects short-lived
# credentials. Association resources are declared at the bottom.
#
# IRSA ALTERNATIVE: if you do not want the pod identity agent, remove the
# `aws_eks_pod_identity_association` resources and instead annotate the service
# accounts with:
#   eks.amazonaws.com/role-arn: <role_arn>
# The IAM roles and trust policies below work for both mechanisms only for Pod
# Identity; IRSA trusts the cluster OIDC provider (sub/aud conditions) rather
# than `pods.eks.amazonaws.com`. This module implements the Pod Identity trust.
#
# Roles:
#   * external-secrets  -> read /<env>/kubecommerce/* secrets (least privilege)
#   * aws-load-balancer-controller -> ELB management (AWS managed policy by
#     default; upstream recommends the scoped `AWSLoadBalancerControllerIAMPolicy`
#     JSON, pass its ARN via `alb_controller_policy_arns` to tighten).
#     Associated with the `platform-system`/`aws-load-balancer-controller`
#     service account (where the GitOps repo installs it).
#   * <optional> app S3 read-only role for a bucket; an association is created
#     with it so the role is actually assumable.
# =============================================================================

data "aws_partition" "current" {}

data "aws_caller_identity" "current" {}

# --- Shared Pod Identity trust policy ---------------------------------------

data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    sid     = "EksPodIdentity"
    effect  = "Allow"
    actions = ["sts:AssumeRole", "sts:TagSession"]

    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
  }
}

# --- External Secrets Operator ----------------------------------------------

resource "aws_iam_role" "external_secrets" {
  count = var.create_external_secrets_role ? 1 : 0

  name                 = "${var.name}-external-secrets"
  description          = "External Secrets Operator read access to KubeCommerce secrets"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_assume.json

  tags = merge(var.tags, { Name = "${var.name}-external-secrets" })
}

data "aws_iam_policy_document" "external_secrets" {
  count = var.create_external_secrets_role ? 1 : 0

  statement {
    sid    = "ReadKubeCommerceSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:DescribeSecret",
      "secretsmanager:ListSecretVersionIds",
    ]
    resources = var.secret_arns
  }

  dynamic "statement" {
    for_each = length(var.kms_key_arns) > 0 ? [1] : []

    content {
      sid       = "DecryptSecretCmk"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = var.kms_key_arns
    }
  }
}

resource "aws_iam_role_policy" "external_secrets" {
  count = var.create_external_secrets_role ? 1 : 0

  name   = "${var.name}-external-secrets"
  role   = aws_iam_role.external_secrets[0].id
  policy = data.aws_iam_policy_document.external_secrets[0].json
}

# --- AWS Load Balancer Controller -------------------------------------------

resource "aws_iam_role" "alb_controller" {
  count = var.create_alb_controller_role ? 1 : 0

  name                 = "${var.name}-aws-load-balancer-controller"
  description          = "AWS Load Balancer Controller"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_assume.json

  tags = merge(var.tags, { Name = "${var.name}-aws-load-balancer-controller" })
}

resource "aws_iam_role_policy_attachment" "alb_controller" {
  for_each = var.create_alb_controller_role ? toset(var.alb_controller_policy_arns) : toset([])

  role       = aws_iam_role.alb_controller[0].name
  policy_arn = each.value
}

# --- Optional application S3 read-only role ---------------------------------

resource "aws_iam_role" "app_s3" {
  count = var.create_app_s3_role ? 1 : 0

  name                 = "${var.name}-app-s3-read"
  description          = "Optional read-only S3 access for an application service"
  max_session_duration = 3600
  assume_role_policy   = data.aws_iam_policy_document.pod_identity_assume.json

  tags = merge(var.tags, { Name = "${var.name}-app-s3-read" })
}

data "aws_iam_policy_document" "app_s3" {
  count = var.create_app_s3_role ? 1 : 0

  statement {
    sid       = "ListBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation"]
    resources = [var.app_s3_bucket_arn]
  }

  statement {
    sid       = "ReadObjects"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.app_s3_bucket_arn}/*"]
  }
}

resource "aws_iam_role_policy" "app_s3" {
  count = var.create_app_s3_role ? 1 : 0

  name   = "${var.name}-app-s3-read"
  role   = aws_iam_role.app_s3[0].id
  policy = data.aws_iam_policy_document.app_s3[0].json
}

# --- Pod Identity associations ----------------------------------------------

resource "aws_eks_pod_identity_association" "external_secrets" {
  count = var.create_external_secrets_role ? 1 : 0

  cluster_name    = var.cluster_name
  namespace       = var.external_secrets_namespace
  service_account = var.external_secrets_service_account
  role_arn        = aws_iam_role.external_secrets[0].arn

  tags = var.tags
}

resource "aws_eks_pod_identity_association" "alb_controller" {
  count = var.create_alb_controller_role ? 1 : 0

  cluster_name    = var.cluster_name
  namespace       = var.alb_controller_namespace
  service_account = var.alb_controller_service_account
  role_arn        = aws_iam_role.alb_controller[0].arn

  tags = var.tags
}

# The S3 role is useless without an association: declare it so the target
# service account can actually assume the role via Pod Identity.
resource "aws_eks_pod_identity_association" "app_s3" {
  count = var.create_app_s3_role ? 1 : 0

  cluster_name    = var.cluster_name
  namespace       = var.app_s3_namespace
  service_account = var.app_s3_service_account
  role_arn        = aws_iam_role.app_s3[0].arn

  tags = var.tags
}
