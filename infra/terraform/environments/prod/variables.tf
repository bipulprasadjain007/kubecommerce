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
  default     = "prod"
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
  default     = 4
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public EKS API endpoint. REPLACE the placeholder with your operator/office CIDR before applying; 0.0.0.0/0 is intentionally not the default."
  type        = list(string)
  # 203.0.113.0/24 is TEST-NET-3 (documentation range) - it is a placeholder so
  # an accidental apply does not silently expose the API to the internet.
  default = ["203.0.113.0/24"]
}

# --- RDS (optional) ---------------------------------------------------------

variable "enable_rds" {
  description = "Create the RDS PostgreSQL instance. On in prod."
  type        = bool
  default     = true
}

variable "rds_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "rds_multi_az" {
  description = "Enable RDS Multi-AZ."
  type        = bool
  default     = true
}

variable "rds_deletion_protection" {
  description = "Block accidental RDS deletion."
  type        = bool
  default     = true
}

variable "rds_skip_final_snapshot" {
  description = "Skip the final snapshot on destroy."
  type        = bool
  default     = false
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
  description = "Create the account-global GitHub OIDC provider. Set false (and supply existing_oidc_provider_arn) when dev or another project already created it."
  type        = bool
  default     = true
}

variable "existing_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN. When non-empty, prod reuses it instead of creating a second provider."
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
  default     = 200
}

variable "budget_alert_emails" {
  description = "Email subscribers for budget alerts (at least one required to plan/apply)."
  type        = list(string)
  default     = []
}
