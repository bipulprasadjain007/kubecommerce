output "budget_name" {
  description = "Budget name."
  value       = aws_budgets_budget.this.name
}

output "budget_id" {
  description = "Budget ID / ARN."
  value       = aws_budgets_budget.this.id
}
