variable "name_prefix" {
  description = "Secrets Manager path prefix, e.g. /dev/kubecommerce."
  type        = string
}

variable "secret_paths" {
  description = "Map of path suffix -> human description for each secret. Must match the paths consumed by the External Secrets overlays."
  type        = map(string)
  default = {
    "database/auth-url"    = "PostgreSQL DSN for auth-service (AUTH_DATABASE_URL)"
    "database/catalog-url" = "PostgreSQL DSN for catalog-service (CATALOG_DATABASE_URL)"
    "database/orders-url"  = "PostgreSQL DSN for order-service (ORDERS_DATABASE_URL)"
    "auth/jwt-signing-key" = "RS256 JWT private key for auth-service (PEM)"
    "redis/catalog-url"    = "Redis URL for catalog-service (CATALOG_REDIS_URL)"
    "redis/worker-url"     = "Redis URL for notification-worker (WORKER_REDIS_URL)"
    "redis/gateway-url"    = "Redis URL for gateway-api (GATEWAY_REDIS_URL)"
    "rabbitmq/orders-url"  = "AMQP URL for order-service (ORDERS_RABBITMQ_URL)"
    "rabbitmq/worker-url"  = "AMQP URL for notification-worker (WORKER_RABBITMQ_URL)"
    "internal/api-token"   = "Shared X-Internal-Token for internal mutating calls (catalog/order/gateway)"
  }
}

variable "placeholder_value" {
  description = "Placeholder written once; ignored on subsequent applies so out-of-band values survive."
  type        = string
  default     = "REPLACE_ME"
  sensitive   = true
}

variable "recovery_window_in_days" {
  description = "Recovery window before deletion. 0 = force delete (dev)."
  type        = number
  default     = 7
}

variable "tags" {
  description = "Tags applied to every resource in this module."
  type        = map(string)
  default     = {}
}
