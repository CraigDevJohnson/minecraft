locals {
  launcher_count = var.launcher == null ? 0 : 1
  # The site may call only backends named *-<its environment>-* (the-lobby's site boundary), so the name carries it.
  launcher_name           = try("${var.resource_prefix}-${var.launcher.site_environment}-launcher", "${var.resource_prefix}-launcher")
  launcher_arn            = "arn:aws:lambda:${var.region}:${var.account_id}:function:${local.launcher_name}"
  launcher_host_arn       = "arn:aws:ec2:${var.region}:${var.account_id}:instance/${try(aws_instance.host[0].id, "absent")}"
  launcher_state_name     = "/${var.resource_prefix}/launcher/state"
  launcher_state_arn      = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${local.launcher_state_name}"
  launcher_token_name     = try(coalesce(var.launcher.cloudflare_token_parameter, "/${var.resource_prefix}/launcher/cloudflare-token"), null)
  launcher_token_arn      = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${coalesce(local.launcher_token_name, "/absent")}"
  launcher_log_group_name = "/aws/lambda/${local.launcher_name}"
}

resource "aws_iam_role" "launcher" {
  count = local.launcher_count
  name  = local.launcher_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
  permissions_boundary = var.launcher.permissions_boundary_arn
  tags                 = { Purpose = "launcher-credentials" }
}

resource "aws_iam_role_policy" "launcher" {
  count = local.launcher_count
  name  = "launcher"
  role  = aws_iam_role.launcher[0].id
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
        Sid      = "StartOnlyThisHost"
        Effect   = "Allow"
        Action   = ["ec2:StartInstances"]
        Resource = [local.launcher_host_arn]
      },
      {
        Sid      = "StartGameOnlyOnThisHostWithShellDocument"
        Effect   = "Allow"
        Action   = ["ssm:SendCommand"]
        Resource = [local.launcher_host_arn, "arn:aws:ssm:${var.region}::document/AWS-RunShellScript"]
      },
      {
        Sid      = "FollowStart"
        Effect   = "Allow"
        Action   = ["ssm:GetCommandInvocation", "ssm:DescribeInstanceInformation"]
        Resource = "*"
      },
      {
        Sid      = "OwnStateOnly"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = [local.launcher_state_arn]
      },
      {
        Sid      = "ReadOnlyCloudflareToken"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.launcher_token_arn]
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
        Sid      = "FinishStartsInItself"
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = [local.launcher_arn]
      },
      {
        Sid      = "OwnLogsOnly"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:${local.launcher_log_group_name}:*"]
      },
      ], var.idle_stop == null ? [] : [{
        Sid      = "ReadIdleStopProgress"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.idle_stop_state_arn]
    }])
  })
}

resource "aws_ssm_parameter" "launcher_state" {
  count       = local.launcher_count
  name        = local.launcher_state_name
  description = "The launcher's last start and its outcome; owned by the function after creation."
  type        = "String"
  value       = "{}"
  lifecycle {
    ignore_changes = [value]
  }
}

resource "aws_cloudwatch_log_group" "launcher" {
  count             = local.launcher_count
  name              = local.launcher_log_group_name
  retention_in_days = 30
}

resource "aws_lambda_function" "launcher" {
  count            = local.launcher_count
  function_name    = local.launcher_name
  description      = "Answers The Lobby's Minecraft Launcher and starts the family server."
  role             = aws_iam_role.launcher[0].arn
  runtime          = "provided.al2023"
  architectures    = ["arm64"]
  handler          = "bootstrap"
  filename         = var.launcher.package_path
  source_code_hash = filebase64sha256(var.launcher.package_path)
  memory_size      = 128
  # A request answers in seconds; the follow-up run of a start waits for EC2, SSM and the game.
  timeout = 900
  environment {
    variables = {
      HOST_ID                    = try(aws_instance.host[0].id, "absent")
      INSTANCE                   = one(var.instance_ids)
      SERVER_NAME                = var.launcher.server_name
      PUBLIC_HOSTNAME            = var.public_hostname
      BEDROCK_PORT               = "19132"
      STATE_PARAMETER            = aws_ssm_parameter.launcher_state[0].name
      CLOUDFLARE_ZONE_ID         = var.cloudflare_zone_id
      CLOUDFLARE_TOKEN_PARAMETER = local.launcher_token_name
      IDLE_STOP_STATE_PARAMETER  = var.idle_stop == null ? "" : local.idle_stop_state_name
    }
  }
  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.launcher[0].name
  }
  depends_on = [aws_iam_role_policy.launcher]
}

# A lost follow-up is reported failed after its deadline and can be started again, so it is never retried
# late into a session somebody else has already handled.
resource "aws_lambda_function_event_invoke_config" "launcher" {
  count                        = local.launcher_count
  function_name                = aws_lambda_function.launcher[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_lambda_function_url" "launcher" {
  count              = local.launcher_count
  function_name      = aws_lambda_function.launcher[0].function_name
  authorization_type = "AWS_IAM"
}

# Only the site's role may call the address (the-lobby ADR 0006). The second statement is the
# lambda:InvokeFunction grant AWS also requires for a Function URL, limited to calls through the URL.
resource "aws_lambda_permission" "launcher_url" {
  count                  = local.launcher_count
  statement_id           = "AllowSiteThroughFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.launcher[0].function_name
  principal              = var.launcher.site_role_arn
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "launcher_invoke" {
  count                    = local.launcher_count
  statement_id             = "AllowSiteInvokeThroughFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.launcher[0].function_name
  principal                = var.launcher.site_role_arn
  invoked_via_function_url = true
}

resource "aws_sns_topic" "launcher_alerts" {
  count = local.launcher_count
  name  = "${local.launcher_name}-alerts"
}

resource "aws_sns_topic_subscription" "launcher_alerts" {
  count     = local.launcher_count
  topic_arn = aws_sns_topic.launcher_alerts[0].arn
  protocol  = "email"
  endpoint  = var.budget_notification_email
}

resource "aws_cloudwatch_metric_alarm" "launcher_errors" {
  count               = local.launcher_count
  alarm_name          = "${local.launcher_name}-failing"
  alarm_description   = "A start from The Lobby failed or could not update DNS. See docs/operations/launcher.md."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.launcher[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.launcher_alerts[0].arn]
  ok_actions          = [aws_sns_topic.launcher_alerts[0].arn]
}

# A start whose follow-up could not be handed off answers 202, so Lambda counts no error. The function
# reports it as this metric in its logs (embedded metric format), and the server shows failed later.
resource "aws_cloudwatch_metric_alarm" "launcher_lost_starts" {
  count               = local.launcher_count
  alarm_name          = "${local.launcher_name}-lost-start"
  alarm_description   = "A start from The Lobby began EC2 but could not hand off the rest of the start. See docs/operations/launcher.md."
  namespace           = "Minecraft/Launcher"
  metric_name         = "LostStarts"
  dimensions          = { FunctionName = aws_lambda_function.launcher[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.launcher_alerts[0].arn]
  ok_actions          = [aws_sns_topic.launcher_alerts[0].arn]
}
