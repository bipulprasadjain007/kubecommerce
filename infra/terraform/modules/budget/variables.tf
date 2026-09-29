variable "name" {
  description = "Budget name prefix."
  type        = string
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget in USD."
  type        = number
  default     = 100
}

variable "alert_thresholds" {
  description = "Percentage-of-budget thresholds that raise ACTUAL-spend alerts."
  type        = list(number)
  default     = [50, 80, 100]
}

variable "alert_on_forecasted" {
  description = "Also alert when forecasted spend reaches 100% of the budget."
  type        = bool
  default     = true
}

variable "alert_emails" {
  description = "Email subscribers for budget alerts. At least one subscriber is required."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.alert_emails) > 0 || length(var.alert_sns_topic_arns) > 0
    error_message = "Provide at least one of alert_emails or alert_sns_topic_arns."
  }
}

variable "alert_sns_topic_arns" {
  description = "SNS topic subscribers for budget alerts."
  type        = list(string)
  default     = []
}

variable "cost_filter_tags" {
  description = "Tag filter so the budget tracks this project rather than the whole account."
  type        = map(string)
  default     = { project = "kubecommerce" }
}
