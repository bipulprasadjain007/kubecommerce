variable "name" {
  description = "Name prefix for roles (e.g. kubecommerce-dev)."
  type        = string
}

variable "cluster_name" {
  description = "EKS cluster name for Pod Identity associations."
  type        = string
}

variable "secret_arns" {
  description = "Secrets Manager secret ARNs the External Secrets Operator may read."
  type        = list(string)
  default     = []
}

variable "kms_key_arns" {
  description = "Optional customer-managed KMS key ARNs used to encrypt the secrets."
  type        = list(string)
  default     = []
}

variable "create_external_secrets_role" {
  description = "Create the External Secrets Operator role + association."
  type        = bool
  default     = true
}

variable "external_secrets_namespace" {
  description = "Namespace of the External Secrets Operator."
  type        = string
  default     = "external-secrets"
}

variable "external_secrets_service_account" {
  description = "Service account name of the External Secrets Operator."
  type        = string
  default     = "external-secrets"
}

variable "create_alb_controller_role" {
  description = "Create the AWS Load Balancer Controller role + association."
  type        = bool
  default     = true
}

variable "alb_controller_namespace" {
  description = "Namespace of the AWS Load Balancer Controller (the GitOps repo installs it into platform-system)."
  type        = string
  default     = "platform-system"
}

variable "alb_controller_service_account" {
  description = "Service account name of the AWS Load Balancer Controller."
  type        = string
  default     = "aws-load-balancer-controller"
}

variable "alb_controller_policy_arns" {
  description = "Policy ARNs attached to the ALB controller role. Replace the AWS managed default with the scoped upstream policy for production."
  type        = list(string)
  default     = ["arn:aws:iam::aws:policy/ElasticLoadBalancingFullAccess"]
}

variable "create_app_s3_role" {
  description = "Create the optional read-only application S3 role."
  type        = bool
  default     = false
}

variable "app_s3_bucket_arn" {
  description = "Bucket ARN for the optional application S3 role (required when create_app_s3_role = true)."
  type        = string
  default     = ""
}

variable "app_s3_namespace" {
  description = "Namespace of the optional application S3 service account."
  type        = string
  default     = "kubecommerce"
}

variable "app_s3_service_account" {
  description = "Service account name for the optional application S3 role."
  type        = string
  default     = "app-s3-reader"
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
