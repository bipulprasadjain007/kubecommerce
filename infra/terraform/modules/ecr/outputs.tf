output "repository_urls" {
  description = "Map of service name -> ECR repository URL (push/pull target)."
  value       = { for name, repo in aws_ecr_repository.this : name => repo.repository_url }
}

output "repository_arns" {
  description = "Map of service name -> ECR repository ARN."
  value       = { for name, repo in aws_ecr_repository.this : name => repo.arn }
}

output "repository_names" {
  description = "Map of service name -> fully qualified repository name."
  value       = { for name, repo in aws_ecr_repository.this : name => repo.name }
}

output "registry_id" {
  description = "AWS account ID owning the repositories."
  value       = values(aws_ecr_repository.this)[0].registry_id
}
