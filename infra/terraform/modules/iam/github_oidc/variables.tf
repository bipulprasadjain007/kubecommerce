variable "name" {
  description = "Name prefix for the role and provider (e.g. kubecommerce-dev)."
  type        = string
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false if the account already has one."
  type        = bool
  default     = true
}

variable "existing_oidc_provider_arn" {
  description = "Existing GitHub OIDC provider ARN, used when create_oidc_provider = false."
  type        = string
  default     = ""
}

variable "allowed_subjects" {
  description = "GitHub OIDC `sub` claims allowed to assume the role. Refs are restricted to main and v* tags; the environment subject covers the protected `aws` environment used by the manual apply job. Do NOT add pull_request wildcards."
  type        = list(string)
  default = [
    "repo:bipulprasadjain007/kubecommerce:ref:refs/heads/main",
    "repo:bipulprasadjain007/kubecommerce:ref:refs/tags/v*",
    "repo:bipulprasadjain007/kubecommerce:environment:aws",
  ]
}

variable "ecr_repository_arns" {
  description = "ECR repository ARNs the role may push to. Empty falls back to \"*\" (not recommended)."
  type        = list(string)
  default     = []
}

variable "additional_policy_arns" {
  description = "Extra managed policy ARNs to attach (opt-in, e.g. Terraform deploy permissions)."
  type        = list(string)
  default     = []
}

variable "max_session_duration" {
  description = "Maximum role session duration in seconds."
  type        = number
  default     = 3600
}

variable "thumbprints" {
  description = "Optional explicit OIDC thumbprints. Empty lets AWS manage the CA."
  type        = list(string)
  default     = []
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
