output "role_arn" {
  description = "CI role ARN to set as the GitHub repository variable AWS_ROLE_ARN."
  value       = aws_iam_role.this.arn
}

output "role_name" {
  description = "CI role name."
  value       = aws_iam_role.this.name
}

output "oidc_provider_arn" {
  description = "GitHub OIDC provider ARN (created or pre-existing)."
  value       = local.oidc_provider_arn
}
