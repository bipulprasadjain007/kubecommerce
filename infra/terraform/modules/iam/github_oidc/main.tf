# =============================================================================
# iam/github_oidc - GitHub Actions OIDC provider + CI role.
#
# No long-lived AWS access keys are used. The role can only be assumed by
# workflows running on:
#
#   repo:bipulprasadjain007/kubecommerce:ref:refs/heads/main
#   repo:bipulprasadjain007/kubecommerce:ref:refs/tags/v*
#   repo:bipulprasadjain007/kubecommerce:environment:aws
#
# The environment subject is what GitHub presents for jobs gated by the
# protected `aws` environment (the manual apply job). Ref subjects stay
# restricted to main and v* tags; no pull_request subjects are allowed.
#
# (override `allowed_subjects` for forks/other owners). The default permission
# set is deliberately ECR-only (push/pull images), matching the release
# workflow `aws-oidc-placeholder` job. Terraform plan/apply needs more than ECR
# push: attach the extra policies through `additional_policy_arns` (for
# example a scoped deployment policy) to the SAME role referenced by the
# repository variable `AWS_ROLE_ARN`, or create a separate role. That opt-in is
# explicit and documented in infra/README.md - the default stays least
# privilege.
# =============================================================================

data "aws_partition" "current" {}

resource "aws_iam_openid_connect_provider" "this" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]

  # GitHub rotates its CA; AWS now manages the thumbprint when the list is
  # empty. Set `thumbprints` only if you need to pin an explicit value.
  thumbprint_list = var.thumbprints

  tags = merge(var.tags, { Name = "${var.name}-github-oidc" })
}

locals {
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.this[0].arn : var.existing_oidc_provider_arn

  ecr_resource_arns = length(var.ecr_repository_arns) > 0 ? var.ecr_repository_arns : ["*"]
}

resource "aws_iam_role" "this" {
  name                 = "${var.name}-github-ci"
  description          = "GitHub Actions OIDC role for KubeCommerce CI"
  max_session_duration = var.max_session_duration

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid    = "GitHubActionsOIDC"
      Effect = "Allow"
      Principal = {
        Federated = local.oidc_provider_arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = var.allowed_subjects
        }
      }
    }]
  })

  tags = merge(var.tags, { Name = "${var.name}-github-ci" })
}

# Least-privilege inline policy: ECR push/pull only.
resource "aws_iam_role_policy" "ecr" {
  name = "${var.name}-ecr-push"
  role = aws_iam_role.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken and DescribeRepositories are account-level and
        # do not support resource-level scoping; scoping them to repo ARNs
        # silently grants nothing.
        Sid      = "EcrAccountLevel"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken", "ecr:DescribeRepositories"]
        Resource = ["*"]
      },
      {
        Sid    = "EcrPushPull"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload",
          "ecr:ListImages",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = local.ecr_resource_arns
      },
    ]
  })
}

# Explicit, opt-in widening (e.g. Terraform deployment). Empty by default.
resource "aws_iam_role_policy_attachment" "additional" {
  for_each = toset(var.additional_policy_arns)

  role       = aws_iam_role.this.name
  policy_arn = each.value
}
