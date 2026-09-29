variable "name" {
  description = "Name prefix for RDS resources (e.g. kubecommerce-prod)."
  type        = string
}

variable "vpc_id" {
  description = "VPC ID where the database lives."
  type        = string
}

variable "subnet_ids" {
  description = "Private subnet IDs for the DB subnet group."
  type        = list(string)
}

variable "allowed_security_group_ids" {
  description = "Security group IDs allowed to reach PostgreSQL (EKS cluster SG)."
  type        = list(string)
}

variable "engine_version" {
  description = "PostgreSQL engine version."
  type        = string
  default     = "16"
}

variable "instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.t4g.micro"
}

variable "allocated_storage" {
  description = "Initial allocated storage in GiB."
  type        = number
  default     = 20
}

variable "max_allocated_storage" {
  description = "Upper storage autoscaling limit in GiB."
  type        = number
  default     = 50
}

variable "database_name" {
  description = "Initial database created by RDS."
  type        = string
  default     = "kubecommerce"
}

variable "master_username" {
  description = "Master username. The password is RDS-managed."
  type        = string
  default     = "kubecommerce"
}

variable "multi_az" {
  description = "Enable Multi-AZ. Off in dev to control cost."
  type        = bool
  default     = false
}

variable "backup_retention_days" {
  description = "Automated backup retention period in days."
  type        = number
  default     = 7
}

variable "deletion_protection" {
  description = "Block accidental deletion of the database."
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Skip the final snapshot on destroy (cheap for dev; false in prod)."
  type        = bool
  default     = true
}

variable "performance_insights_enabled" {
  description = "Enable Performance Insights (extra cost)."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
