# =============================================================================
# rds - optional PostgreSQL 16 in the private subnets.
#
# The module is created by the root module only when `enable_rds = true` (dev
# has it off, prod on). Cost controls:
#   * `multi_az` defaults to false (single-AZ); prod can enable it.
#   * db.t4g.micro is the smallest burstable instance by default.
#
# Password handling: `manage_master_user_password = true` asks RDS to create
# and rotate the master password in AWS Secrets Manager. Terraform never sees
# or stores the plaintext password, only the managed secret ARN
# (`master_user_secret[0].secret_arn`). The External Secrets Operator reads
# that secret (see modules/iam/pod_identity) and projects it into the cluster.
#
# This is a SINGLE database instance hosting multiple databases. The five
# services need auth_db, catalog_db and orders_db; create them after apply
# (documented in infra/README.md) or point each service's DATABASE_URL at the
# same instance with a different database name.
# =============================================================================

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.subnet_ids

  tags = merge(var.tags, { Name = "${var.name}-db" })
}

resource "aws_security_group" "this" {
  name        = "${var.name}-rds"
  description = "RDS PostgreSQL access from the cluster"
  vpc_id      = var.vpc_id

  ingress {
    description     = "PostgreSQL from EKS cluster security group"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = var.allowed_security_group_ids
  }

  egress {
    description = "Outbound (VPC endpoints / AWS APIs)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name}-rds" })
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  # Force SSL in transit for every client connection.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = var.tags
}

resource "aws_db_instance" "this" {
  identifier = "${var.name}-pg"

  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.database_name
  username = var.master_username
  port     = 5432

  # RDS creates and rotates the master password in Secrets Manager. The
  # plaintext never enters Terraform state.
  manage_master_user_password = true

  multi_az               = var.multi_az
  db_subnet_group_name   = aws_db_subnet_group.this.name
  parameter_group_name   = aws_db_parameter_group.this.name
  vpc_security_group_ids = [aws_security_group.this.id]
  publicly_accessible    = false

  backup_retention_period = var.backup_retention_days
  deletion_protection     = var.deletion_protection
  skip_final_snapshot     = var.skip_final_snapshot

  # Small demo environment: no point spending on Performance Insights.
  performance_insights_enabled = var.performance_insights_enabled

  apply_immediately = true

  tags = merge(var.tags, { Name = "${var.name}-pg" })
}
