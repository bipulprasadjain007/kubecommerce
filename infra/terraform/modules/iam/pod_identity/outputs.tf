output "external_secrets_role_arn" {
  description = "IAM role ARN for the External Secrets Operator (empty if not created)."
  value       = try(aws_iam_role.external_secrets[0].arn, "")
}

output "alb_controller_role_arn" {
  description = "IAM role ARN for the AWS Load Balancer Controller (empty if not created)."
  value       = try(aws_iam_role.alb_controller[0].arn, "")
}

output "app_s3_role_arn" {
  description = "IAM role ARN for the optional application S3 read access (empty if not created)."
  value       = try(aws_iam_role.app_s3[0].arn, "")
}

output "external_secrets_association_id" {
  description = "EKS Pod Identity association ID for the External Secrets Operator."
  value       = try(aws_eks_pod_identity_association.external_secrets[0].association_id, "")
}

output "alb_controller_association_id" {
  description = "EKS Pod Identity association ID for the AWS Load Balancer Controller."
  value       = try(aws_eks_pod_identity_association.alb_controller[0].association_id, "")
}

output "app_s3_association_id" {
  description = "EKS Pod Identity association ID for the optional application S3 role."
  value       = try(aws_eks_pod_identity_association.app_s3[0].association_id, "")
}
