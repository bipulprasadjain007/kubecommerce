# Default tags are applied to every resource that supports tagging, satisfying
# the guide's requirement: project, environment, owner, managed-by=terraform.
provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = var.project
      environment = var.environment
      owner       = var.owner
      managed-by  = "terraform"
    }
  }
}
