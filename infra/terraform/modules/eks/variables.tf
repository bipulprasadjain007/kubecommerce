variable "name" {
  description = "EKS cluster name and resource name prefix (e.g. kubecommerce-dev)."
  type        = string
}

variable "cluster_version" {
  description = "EKS Kubernetes minor version."
  type        = string
  default     = "1.34"
}

variable "subnet_ids" {
  description = "Private subnet IDs for the control plane ENIs and worker nodes."
  type        = list(string)
}

variable "endpoint_public_access" {
  description = "Expose the Kubernetes API endpoint publicly."
  type        = bool
  default     = true
}

variable "endpoint_private_access" {
  description = "Enable the private Kubernetes API endpoint."
  type        = bool
  default     = true
}

variable "public_access_cidrs" {
  description = "CIDRs allowed to reach the public API endpoint. Restrict in prod."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "enabled_cluster_log_types" {
  description = "Control-plane log types. Default api/audit/authenticator."
  type        = list(string)
  default     = ["api", "audit", "authenticator"]
}

variable "enable_cluster_log_group" {
  description = "Pre-create the CloudWatch log group with a retention policy."
  type        = bool
  default     = true
}

variable "cluster_log_retention_days" {
  description = "Retention for the EKS control-plane log group."
  type        = number
  default     = 30
}

variable "oidc_thumbprints" {
  description = "TLS thumbprints for the OIDC provider. Empty = let AWS manage the CA."
  type        = list(string)
  default     = []
}

variable "node_instance_types" {
  description = "Instance types for the managed node group."
  type        = list(string)
  default     = ["t3.medium"]
}

variable "node_capacity_type" {
  description = "ON_DEMAND or SPOT."
  type        = string
  default     = "ON_DEMAND"

  validation {
    condition     = contains(["ON_DEMAND", "SPOT"], var.node_capacity_type)
    error_message = "node_capacity_type must be ON_DEMAND or SPOT."
  }
}

variable "node_ami_type" {
  description = "AMI type for the managed node group."
  type        = string
  default     = "AL2023_x86_64_STANDARD"
}

variable "node_disk_size" {
  description = "Root disk size (GiB) for worker nodes."
  type        = number
  default     = 20
}

variable "node_desired_size" {
  description = "Desired worker node count."
  type        = number
  default     = 2
}

variable "node_min_size" {
  description = "Minimum worker node count."
  type        = number
  default     = 1
}

variable "node_max_size" {
  description = "Maximum worker node count."
  type        = number
  default     = 3
}

variable "node_labels" {
  description = "Additional labels for the managed node group."
  type        = map(string)
  default     = {}
}

variable "cluster_addons" {
  description = "Map of EKS addon name -> version (\"\" = EKS default)."
  type        = map(string)
  default = {
    "vpc-cni"                = ""
    "kube-proxy"             = ""
    "coredns"                = ""
    "eks-pod-identity-agent" = ""
  }
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
