variable "name" {
  description = "Name prefix for all network resources (e.g. kubecommerce-dev)."
  type        = string
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. /20 or larger leaves room for 16 subnets."
  type        = string
  default     = "10.0.0.0/16"
}

variable "az_count" {
  description = "Number of availability zones / subnet pairs to create."
  type        = number
  default     = 3

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 6
    error_message = "az_count must be between 2 and 6."
  }
}

variable "availability_zones" {
  description = "Optional explicit AZ list. Empty = auto-select az_count AZs in the region."
  type        = list(string)
  default     = []
}

variable "single_nat_gateway" {
  description = "Use one NAT gateway for all private subnets (cheaper) instead of one per AZ."
  type        = bool
  default     = true
}

variable "enable_flow_logs" {
  description = "Create VPC flow logs into CloudWatch Logs. Off by default to control cost."
  type        = bool
  default     = false
}

variable "flow_logs_retention_days" {
  description = "Retention for the optional VPC flow log group."
  type        = number
  default     = 14
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
