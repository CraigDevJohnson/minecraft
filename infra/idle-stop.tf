locals {
  idle_stop_count          = var.idle_stop == null ? 0 : 1
  idle_stop_name           = "${var.resource_prefix}-idle-stop"
  idle_stop_log_group_name = "/aws/lambda/${local.idle_stop_name}"
  idle_stop_package_path   = var.package_dir == null ? try(var.idle_stop.package_path, null) : "${var.package_dir}/idle-stop.zip"
  # The same Cloudflare token SecureString the launcher reads, so there is one copy of the token.
  idle_stop_token_name = coalesce(local.launcher_token_name, "/${var.resource_prefix}/launcher/cloudflare-token")
  idle_stop_token_arn  = "${local.parameter_arn_base}${local.idle_stop_token_name}"
  # The launcher's boundary allows everything the function does on the world hosts, so the operator can create its role.
  idle_stop_boundary = try(coalesce(var.idle_stop.permissions_boundary_arn, try(var.launcher.permissions_boundary_arn, null)), null)
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
  permissions_boundary = local.idle_stop_boundary
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
        Sid      = "StopOnlyWorldHosts"
        Effect   = "Allow"
        Action   = ["ec2:StopInstances"]
        Resource = local.world_host_arns
      },
      {
        Sid      = "DrainOnlyWorldHostsWithShellDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = concat(local.world_host_arns, ["arn:aws:ssm:${var.region}::document/AWS-RunShellScript"])
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
        Resource = [for id in local.world_ids : "${local.parameter_arn_base}${aws_ssm_parameter.world_idle_stop_state[id].name}"]
      },
      {
        Sid      = "ReadOnlyCloudflareToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.idle_stop_token_arn]
      },
      {
        # SecureStrings use the AWS managed aws/ssm key; decrypt only that key, only through SSM.
        Sid      = "DecryptTokenThroughSsmOnly"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = ["arn:aws:kms:${var.region}:${var.account_id}:key/*"]
        Condition = {
          StringEquals               = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" }
          "ForAnyValue:StringEquals" = { "kms:ResourceAliases" = "alias/aws/ssm" }
        }
      },
      {
        Sid      = "OwnLogsOnly"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:${local.idle_stop_log_group_name}:*"]
      },
      ], var.launcher == null ? [] : [{
        # A stop the launcher is running is left to it, so the two never drain a host at once.
        Sid      = "ReadLauncherProgress"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [for id in local.world_ids : "${local.parameter_arn_base}${aws_ssm_parameter.world_launcher_state[id].name}"]
    }])
  })
}

resource "aws_cloudwatch_log_group" "idle_stop" {
  count             = local.idle_stop_count
  name              = local.idle_stop_log_group_name
  retention_in_days = 14
}

resource "aws_lambda_function" "idle_stop" {
  count            = local.idle_stop_count
  function_name    = local.idle_stop_name
  description      = "Drains and stops a world's Minecraft host after it has been idle."
  role             = aws_iam_role.idle_stop[0].arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = local.idle_stop_package_path
  source_code_hash = filebase64sha256(local.idle_stop_package_path)
  memory_size      = 128
  timeout          = 60
  environment {
    variables = {
      WORLDS                     = local.worlds
      CLOUDFLARE_ZONE_ID         = var.cloudflare_zone_id
      CLOUDFLARE_TOKEN_PARAMETER = local.idle_stop_token_name
      IDLE_MINUTES               = tostring(var.idle_stop.idle_minutes)
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
  description         = "Checks each world's Minecraft host for idleness."
  schedule_expression = "rate(5 minutes)"
}

# The one target the family host's check had becomes the first world's, so the change removes nothing.
moved {
  from = aws_cloudwatch_event_target.idle_stop[0]
  to   = aws_cloudwatch_event_target.idle_stop["survival"]
}

# One check per world on every tick, each told which world it is for.
resource "aws_cloudwatch_event_target" "idle_stop" {
  for_each = toset(local.world_idle_keys)
  rule     = aws_cloudwatch_event_rule.idle_stop[0].name
  arn      = aws_lambda_function.idle_stop[0].arn
  input    = jsonencode({ world = each.key })
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
  alarm_description   = "The idle stop failed or is blocked; a world's host may be left running. See docs/operations/idle-stop.md."
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
