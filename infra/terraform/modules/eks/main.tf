# =============================================================================
# eks - EKS control plane, managed node group, OIDC provider and addons.
#
# Design notes:
#   * Managed node group in the private subnets only. Instances default to
#     t3.medium x2 (small, cheap; see infra/README.md "Cost guardrails").
#   * The OIDC provider is created here and consumed by the pod_identity module
#     through the `oidc_provider_arn` output.
#   * Cluster control-plane logging defaults to api/audit/authenticator (the
#     three that matter for security investigations); it can be widened.
#   * `eks-pod-identity-agent` is installed as an addon so EKS Pod Identity
#     associations (modules/iam/pod_identity) work. If you prefer IRSA, remove
#     that addon and attach the roles from pod_identity to Kubernetes service
#     accounts via annotations instead (see comments in that module).
# =============================================================================

data "aws_partition" "current" {}

data "aws_region" "current" {}

# --- Control plane IAM ------------------------------------------------------

resource "aws_iam_role" "cluster" {
  name = "${var.name}-eks-cluster"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

# --- Optional control-plane logs --------------------------------------------

resource "aws_cloudwatch_log_group" "cluster" {
  count = var.enable_cluster_log_group ? 1 : 0

  name              = "/aws/eks/${var.name}/cluster"
  retention_in_days = var.cluster_log_retention_days

  tags = var.tags
}

# --- Cluster ----------------------------------------------------------------

resource "aws_eks_cluster" "this" {
  name     = var.name
  version  = var.cluster_version
  role_arn = aws_iam_role.cluster.arn

  enabled_cluster_log_types = var.enabled_cluster_log_types

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_public_access  = var.endpoint_public_access
    endpoint_private_access = var.endpoint_private_access
    public_access_cidrs     = var.public_access_cidrs
  }

  # Do not destroy the cluster before its node group/addons.
  depends_on = [
    aws_iam_role_policy_attachment.cluster,
    aws_cloudwatch_log_group.cluster,
  ]

  tags = merge(var.tags, { Name = var.name })
}

# --- OIDC provider (for IRSA / GitHub OIDC consumers) -----------------------

resource "aws_iam_openid_connect_provider" "this" {
  url            = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]

  # `thumbprint_list` is Optional+Computed in AWS provider v6: passing an empty
  # list (our default) leaves the thumbprint out of the request and AWS applies
  # the managed CA thumbprint, which it keeps up to date. Pin explicit
  # thumbprints via `oidc_thumbprints` only if a policy requires it.
  thumbprint_list = var.oidc_thumbprints

  tags = merge(var.tags, { Name = "${var.name}-oidc" })
}

# --- Managed node group IAM -------------------------------------------------

resource "aws_iam_role" "node" {
  name = "${var.name}-eks-node"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryReadOnly",
  ])

  role       = aws_iam_role.node.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/${each.value}"
}

# --- Managed node group -----------------------------------------------------

resource "aws_eks_node_group" "this" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name}-default"
  node_role_arn   = aws_iam_role.node.arn

  subnet_ids     = var.subnet_ids
  instance_types = var.node_instance_types
  capacity_type  = var.node_capacity_type
  ami_type       = var.node_ami_type
  disk_size      = var.node_disk_size

  scaling_config {
    desired_size = var.node_desired_size
    min_size     = var.node_min_size
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = merge(var.node_labels, { role = "default" })

  # Don't fight the cluster autoscaler / manual scaling on drift.
  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]

  tags = merge(var.tags, { Name = "${var.name}-default" })
}

# --- Addons -----------------------------------------------------------------
# `cluster_addons` is a map of addon name -> version ("" = EKS default). The
# EKS pod identity agent is included so Pod Identity associations can be used.
resource "aws_eks_addon" "this" {
  for_each = var.cluster_addons

  cluster_name  = aws_eks_cluster.this.name
  addon_name    = each.key
  addon_version = each.value != "" ? each.value : null

  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "PRESERVE"

  tags = merge(var.tags, { Name = "${var.name}-${each.key}" })

  depends_on = [aws_eks_node_group.this]
}
