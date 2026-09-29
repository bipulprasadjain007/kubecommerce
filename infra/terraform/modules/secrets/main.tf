# =============================================================================
# secrets - AWS Secrets Manager paths for the KubeCommerce platform.
#
# Path convention (guide section 6.2), rooted at the environment prefix:
#
#   /<env>/kubecommerce/database/auth-url
#   /<env>/kubecommerce/database/catalog-url
#   /<env>/kubecommerce/database/orders-url
#   /<env>/kubecommerce/auth/jwt-signing-key
#   /<env>/kubecommerce/redis/catalog-url
#   /<env>/kubecommerce/redis/worker-url
#   /<env>/kubecommerce/redis/gateway-url
#   /<env>/kubecommerce/rabbitmq/orders-url
#   /<env>/kubecommerce/rabbitmq/worker-url
#   /<env>/kubecommerce/internal/api-token
#
# Values are created out of band (user/CI/console) - the placeholder written
# here exists only so the External Secrets Operator can resolve the path
# immediately. `lifecycle { ignore_changes = [secret_string] }` means Terraform
# never overwrites a real value with the placeholder on later applies.
#
# Do NOT put real secrets in tfvars or commit them. See infra/README.md.
# =============================================================================

resource "aws_secretsmanager_secret" "this" {
  for_each = var.secret_paths

  name                    = "${var.name_prefix}/${each.key}"
  description             = each.value
  recovery_window_in_days = var.recovery_window_in_days

  tags = merge(var.tags, { Name = "${var.name_prefix}/${each.key}" })
}

resource "aws_secretsmanager_secret_version" "placeholder" {
  for_each = var.secret_paths

  secret_id     = aws_secretsmanager_secret.this[each.key].id
  secret_string = var.placeholder_value

  lifecycle {
    # Never clobber a value set out of band with the placeholder.
    ignore_changes = [secret_string]
  }
}
