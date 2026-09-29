# -----------------------------------------------------------------------------
# Outputs consumed by CI and by the GitOps repository (see infra/README.md).
# -----------------------------------------------------------------------------

output "environment" {
  description = "Environment name."
  value       = var.environment
}

output "vpc_id" {
  description = "VPC ID."
  value       = module.network.vpc_id
}

output "private_subnet_ids" {
  description = "Private subnet IDs (EKS nodes / RDS)."
  value       = module.network.private_subnet_ids
}

# --- EKS --------------------------------------------------------------------

output "eks_cluster_name" {
  description = "EKS cluster name."
  value       = module.eks.cluster_name
}

output "eks_cluster_endpoint" {
  description = "EKS API server endpoint."
  value       = module.eks.cluster_endpoint
}

output "eks_oidc_provider_arn" {
  description = "Cluster OIDC provider ARN."
  value       = module.eks.oidc_provider_arn
}

output "eks_kubeconfig_command" {
  description = "Command that writes a kubeconfig entry for the cluster."
  value       = module.eks.kubeconfig_command
}

# --- ECR --------------------------------------------------------------------

output "ecr_repository_urls" {
  description = "Map of service -> ECR repository URL (use as IMAGE_REGISTRY/<service>)."
  value       = module.ecr.repository_urls
}

output "ecr_registry" {
  description = "ECR registry host for this environment (e.g. <account>.dkr.ecr.<region>.amazonaws.com)."
  value       = split("/", values(module.ecr.repository_urls)[0])[0]
}

# --- Secrets ----------------------------------------------------------------

output "secrets_path_prefix" {
  description = "Secrets Manager prefix consumed by External Secrets Operator."
  value       = module.secrets.path_prefix
}

output "secret_arns" {
  description = "Map of secret path suffix -> Secrets Manager ARN."
  value       = module.secrets.secret_arns
}

# --- RDS (only when enabled) ------------------------------------------------

output "rds_address" {
  description = "RDS hostname (null when enable_rds = false)."
  value       = try(module.rds[0].address, null)
}

output "rds_port" {
  description = "RDS port (null when enable_rds = false)."
  value       = try(module.rds[0].port, null)
}

output "rds_master_secret_arn" {
  description = "RDS-managed master password secret ARN (null when enable_rds = false)."
  value       = try(module.rds[0].master_user_secret_arn, null)
  sensitive   = true
}

# --- IAM / CI ---------------------------------------------------------------

output "github_oidc_role_arn" {
  description = "CI role ARN for the GitHub repository variable AWS_ROLE_ARN."
  value       = module.github_oidc.role_arn
}

output "external_secrets_role_arn" {
  description = "External Secrets Operator Pod Identity role ARN."
  value       = module.pod_identity.external_secrets_role_arn
}

output "alb_controller_role_arn" {
  description = "AWS Load Balancer Controller Pod Identity role ARN."
  value       = module.pod_identity.alb_controller_role_arn
}

# --- Budget -----------------------------------------------------------------

output "budget_name" {
  description = "AWS Budget name."
  value       = module.budget.budget_name
}
