variable "name_prefix" {
  description = "Repository namespace prefix (e.g. kubecommerce or kubecommerce-dev)."
  type        = string
}

variable "repository_names" {
  description = "Service repository names (without the prefix)."
  type        = list(string)
  default = [
    "auth-service",
    "catalog-service",
    "order-service",
    "notification-worker",
    "gateway-api",
  ]
}

variable "untagged_image_days" {
  description = "Expire untagged images after this many days."
  type        = number
  default     = 7
}

variable "max_tagged_images" {
  description = "Maximum tagged images to retain per repository."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
