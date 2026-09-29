variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project tag."
  type        = string
  default     = "kubecommerce"
}

variable "environment" {
  description = "Environment name (dev/staging/prod)."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Owner tag."
  type        = string
  default     = "bipulprasadjain007"
}

# --- Network ----------------------------------------------------------------

variable "vpc_cidr" {
  description = "VPC CIDR block."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of AZs / subnet pairs."
  type        = number
  default     = 3
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway (cheaper)."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "Enable VPC flow logs (off by default: CloudWatch ingest cost)."
  type        = bool
  default     = false
}

# --- EKS --------------------------------------------------------------------

variable "eks_cluster_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.34"
}

variable "node_instance_types" {
  description = "Managed node group instance types."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "node_desired_size" {
  description = "Desired worker nodes."
  type        = number
  default     = 2
}

variable "node_min_size" {
  description = "Minimum worker nodes."
  type        = number
  default     = 1
}

variable "node_max_size" {
  description = "Maximum worker nodes."
  type        = number
  default     = 3
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint. Dev opens it for convenience; tighten for anything longer-lived."
  type        = list(string)
  # 0.0.0.0/0 is acceptable ONLY for a short-lived dev/demo cluster.
  default = ["0.0.0.0/0"]
}

# --- RDS (optional) ---------------------------------------------------------

variable "enable_rds" {
  description = "Create the RDS PostgreSQL instance. Off in dev to control cost."
  type        = bool
  default     = false
}

variable "rds_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "rds_multi_az" {
  description = "Enable RDS Multi-AZ."
  type        = bool
  default     = false
}

variable "rds_deletion_protection" {
  description = "Block accidental RDS deletion."
  type        = bool
  default     = false
}

variable "rds_skip_final_snapshot" {
  description = "Skip the final snapshot on destroy."
  type        = bool
  default     = true
}

# --- CI / IAM ---------------------------------------------------------------

variable "github_oidc_subjects" {
  description = "GitHub OIDC subject claims allowed to assume the CI role. plan runs on main; apply runs under the protected `aws` environment."
  type        = list(string)
  default = [
    "repo:bipulprasadjain007/kubecommerce:ref:refs/heads/main",
    "repo:bipulprasadjain007/kubecommerce:ref:refs/tags/v*",
    "repo:bipulprasadjain007/kubecommerce:environment:aws",
  ]
}

variable "create_oidc_provider" {
  description = "Create the account-global GitHub OIDC provider. Set false and provide existing_oidc_provider_arn when another environment (or project) already created it."
  type        = bool
  default     = true
}

variable "existing_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN to reuse (required when create_oidc_provider = false)."
  type        = string
  default     = ""
}

variable "terraform_role_policy_arns" {
  description = "Additional managed policies attached to the CI role (opt-in, for terraform plan/apply)."
  type        = list(string)
  default     = []
}

# --- Budget -----------------------------------------------------------------

variable "monthly_budget_usd" {
  description = "Monthly AWS budget in USD."
  type        = number
  default     = 100
}

variable "budget_alert_emails" {
  description = "Email subscribers for budget alerts (at least one required to plan/apply)."
  type        = list(string)
  default     = []
}
