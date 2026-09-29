output "secret_arns" {
  description = "Map of path suffix -> secret ARN."
  value       = { for path, secret in aws_secretsmanager_secret.this : path => secret.arn }
}

output "secret_names" {
  description = "Map of path suffix -> full secret name."
  value       = { for path, secret in aws_secretsmanager_secret.this : path => secret.name }
}

output "path_prefix" {
  description = "Prefix to use as the ExternalSecret `spec.secretStore` path pattern."
  value       = var.name_prefix
}

output "secret_arn_prefix" {
  description = "Wildcard ARN covering every secret in this prefix, for IAM policies."
  value       = "arn:aws:secretsmanager:*:*:secret:${trimprefix(var.name_prefix, "/")}/*"
}
