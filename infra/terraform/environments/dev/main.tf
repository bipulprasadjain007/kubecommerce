locals {
  name_prefix = "kubecommerce-${var.environment}"
  common_tags = {
    project     = var.project
    environment = var.environment
    owner       = var.owner
    managed-by  = "terraform"
  }
}

module "network" {
  source = "../../modules/network"

  name               = local.name_prefix
  vpc_cidr           = var.vpc_cidr
  az_count           = var.az_count
  single_nat_gateway = var.single_nat_gateway
  enable_flow_logs   = var.enable_flow_logs
  tags               = local.common_tags
}

module "ecr" {
  source = "../../modules/ecr"

  name_prefix = local.name_prefix
  tags        = local.common_tags
}

module "eks" {
  source = "../../modules/eks"

  name                = local.name_prefix
  cluster_version     = var.eks_cluster_version
  subnet_ids          = module.network.private_subnet_ids
  public_access_cidrs = var.public_access_cidrs
  node_instance_types = var.node_instance_types
  node_desired_size   = var.node_desired_size
  node_min_size       = var.node_min_size
  node_max_size       = var.node_max_size
  tags                = local.common_tags
}

module "secrets" {
  source = "../../modules/secrets"

  name_prefix = "/${var.environment}/kubecommerce"
  tags        = local.common_tags
}

module "github_oidc" {
  source = "../../modules/iam/github_oidc"

  name                       = local.name_prefix
  ecr_repository_arns        = values(module.ecr.repository_arns)
  allowed_subjects           = var.github_oidc_subjects
  additional_policy_arns     = var.terraform_role_policy_arns
  create_oidc_provider       = var.create_oidc_provider
  existing_oidc_provider_arn = var.existing_oidc_provider_arn
  tags                       = local.common_tags
}

module "pod_identity" {
  source = "../../modules/iam/pod_identity"

  name         = local.name_prefix
  cluster_name = module.eks.cluster_name
  # KubeCommerce secrets plus (when present) the RDS-managed master secret.
  secret_arns = concat(
    values(module.secrets.secret_arns),
    var.enable_rds && try(module.rds[0].master_user_secret_arn, "") != "" ? [module.rds[0].master_user_secret_arn] : [],
  )
  tags = local.common_tags
}

module "rds" {
  source = "../../modules/rds"
  count  = var.enable_rds ? 1 : 0

  name                       = local.name_prefix
  vpc_id                     = module.network.vpc_id
  subnet_ids                 = module.network.private_subnet_ids
  allowed_security_group_ids = [module.eks.cluster_security_group_id]
  instance_class             = var.rds_instance_class
  multi_az                   = var.rds_multi_az
  deletion_protection        = var.rds_deletion_protection
  skip_final_snapshot        = var.rds_skip_final_snapshot
  tags                       = local.common_tags
}

module "budget" {
  source = "../../modules/budget"

  name               = local.name_prefix
  monthly_budget_usd = var.monthly_budget_usd
  alert_emails       = var.budget_alert_emails
  cost_filter_tags   = { project = var.project }
}
