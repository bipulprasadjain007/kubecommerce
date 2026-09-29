# =============================================================================
# budget - AWS Budget with cost alerts so spend cannot run away unnoticed.
#
# Defaults to a small monthly limit and alerts at 50/80/100% of actual spend
# plus 100% of forecasted spend. At least one subscriber (email or SNS topic)
# must be provided by the caller.
# =============================================================================

locals {
  notifications = concat(
    [for pct in var.alert_thresholds : { type = "ACTUAL", pct = pct }],
    var.alert_on_forecasted ? [{ type = "FORECASTED", pct = 100 }] : [],
  )
}

resource "aws_budgets_budget" "this" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Explicit dates keep the budget active; 2087 is the AWS max.
  time_period_start = "2024-01-01_00:00"
  time_period_end   = "2087-06-15_00:00"

  dynamic "cost_filter" {
    for_each = length(var.cost_filter_tags) > 0 ? [1] : []

    content {
      name = "TagKeyValue"
      # `format` avoids the HCL interpolation trap: `$${...}` is an escape for a
      # literal `${`, which produced the tag value "project${v}" and matched
      # nothing. AWS expects `user:<TagKey>$<TagValue>`.
      values = [for k, v in var.cost_filter_tags : format("user:%s$%s", k, v)]
    }
  }

  dynamic "notification" {
    for_each = local.notifications

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value.pct
      threshold_type             = "PERCENTAGE"
      notification_type          = notification.value.type
      subscriber_email_addresses = var.alert_emails
      subscriber_sns_topic_arns  = var.alert_sns_topic_arns
    }
  }
}
