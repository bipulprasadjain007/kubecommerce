terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # ---------------------------------------------------------------------------
  # Remote state backend (S3 + DynamoDB locking).
  #
  # Bootstrap it once with the commands in infra/README.md, then uncomment and
  # fill in the bucket/table names. The bucket is not secret; it must be unique
  # per account. Never commit real secret values to a tfvars file.
  # ---------------------------------------------------------------------------
  # backend "s3" {
  #   bucket         = "kubecommerce-tfstate-<aws-account-id>"
  #   key            = "kubecommerce/prod/terraform.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "kubecommerce-tfstate-lock"
  #   encrypt        = true
  # }
}
