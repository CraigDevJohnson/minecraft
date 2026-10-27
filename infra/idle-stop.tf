locals {
  idle_stop_count          = var.idle_stop == null ? 0 : 1
  idle_stop_name           = "${var.resource_prefix}-idle-stop"
  idle_stop_host_arn       = "arn:aws:ec2:${var.region}:${var.account_id}:instance/${aws_instance.host.id}"
  idle_stop_state_name     = "/${var.resource_prefix}/idle-stop/state"
  idle_stop_state_arn      = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${local.idle_stop_state_name}"
  idle_stop_log_group_name = "/aws/lambda/${local.idle_stop_name}"
  idle_stop_package_path   = var.package_dir == null ? try(var.idle_stop.package_path, null) : "${var.package_dir}/idle-stop.zip"
}

resource "aws_iam_role" "idle_stop" {
  count = local.idle_stop_count
  name  = local.idle_stop_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
  permissions_boundary = var.idle_stop.permissions_boundary_arn
  tags                 = { Purpose = "idle-stop-credentials" }
}

resource "aws_iam_role_policy" "idle_stop" {
  count = local.idle_stop_count
  name  = "idle-stop"
  role  = aws_iam_role.idle_stop[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "ObserveHost"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Sid      = "StopOnlyThisHost"
        Effect   = "Allow"
        Action   = ["ec2:StopInstances"]
        Resource = [local.idle_stop_host_arn]
      },
      {
        Sid      = "DrainOnlyThisHostWithShellDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = [local.idle_stop_host_arn, "arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
      },
      {
        Sid      = "FollowDrain"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation"]
        Resource = "*"
      },
      {
        Sid      = "OwnStateOnly"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = [local.idle_stop_state_arn]
      },
      {
        Sid      = "ReadOnlyCloudflareToken"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = [var.idle_stop.cloudflare_token_secret_arn]
      },
      {
        Sid      = "OwnLogsOnly"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:${local.idle_stop_log_group_name}:*"]
      },
      ], var.launcher == null ? [] : [{
        # A stop the launcher is running is left to it, so the two never drain the host at once.
        Sid      = "ReadLauncherProgress"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.launcher_state_arn]
    }])
  })
}

resource "aws_ssm_parameter" "idle_stop_state" {
  count       = local.idle_stop_count
  name        = local.idle_stop_state_name
  description = "Idle-stop timer and drain progress; owned by the function after creation."
  type        = "String"
  value       = "{}"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_cloudwatch_log_group" "idle_stop" {
  count             = local.idle_stop_count
  name              = local.idle_stop_log_group_name
  retention_in_days = 14
}

resource "aws_lambda_function" "idle_stop" {
  count                          = local.idle_stop_count
  function_name                  = local.idle_stop_name
  description                    = "Drains and stops the Minecraft host after it has been idle."
  role                           = aws_iam_role.idle_stop[0].arn
  runtime                        = "provided.al2023"
  architectures                  = ["arm64"]
  handler                        = "bootstrap"
  filename                       = local.idle_stop_package_path
  source_code_hash               = filebase64sha256(local.idle_stop_package_path)
  memory_size                    = 128
  timeout                        = 60
  reserved_concurrent_executions = 1
  environment {
    variables = {
      HOST_ID                     = aws_instance.host.id
      STATE_PARAMETER             = aws_ssm_parameter.idle_stop_state[0].name
      PUBLIC_HOSTNAME             = var.public_hostname
      CLOUDFLARE_ZONE_ID          = var.cloudflare_zone_id
      CLOUDFLARE_TOKEN_SECRET_ARN = var.idle_stop.cloudflare_token_secret_arn
      IDLE_MINUTES                = tostring(var.idle_stop.idle_minutes)
      INSTANCES                   = join(",", sort(tolist(var.instance_ids)))
      LAUNCHER_STATE_PARAMETER    = var.launcher == null ? "" : local.launcher_state_name
    }
  }
  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.idle_stop[0].name
  }
  depends_on = [aws_iam_role_policy.idle_stop]
}

resource "aws_cloudwatch_event_rule" "idle_stop" {
  count               = local.idle_stop_count
  name                = local.idle_stop_name
  description         = "Checks the Minecraft host for idleness."
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "idle_stop" {
  count = local.idle_stop_count
  rule  = aws_cloudwatch_event_rule.idle_stop[0].name
  arn   = aws_lambda_function.idle_stop[0].arn
}

resource "aws_lambda_permission" "idle_stop" {
  count         = local.idle_stop_count
  statement_id  = "AllowScheduledIdleCheck"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.idle_stop[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.idle_stop[0].arn
}

resource "aws_sns_topic" "idle_stop_alerts" {
  count = local.idle_stop_count
  name  = "${local.idle_stop_name}-alerts"
}

resource "aws_sns_topic_subscription" "idle_stop_alerts" {
  count     = local.idle_stop_count
  topic_arn = aws_sns_topic.idle_stop_alerts[0].arn
  protocol  = "email"
  endpoint  = var.budget_notification_email
}

resource "aws_cloudwatch_metric_alarm" "idle_stop_errors" {
  count               = local.idle_stop_count
  alarm_name          = "${local.idle_stop_name}-failing"
  alarm_description   = "The idle stop failed or is blocked; the host may be left running. See docs/operations/idle-stop.md."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.idle_stop[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.idle_stop_alerts[0].arn]
  ok_actions          = [aws_sns_topic.idle_stop_alerts[0].arn]
}
