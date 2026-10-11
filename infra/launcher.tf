locals {
  # One function per Lobby environment the launcher serves (var.launcher.sites), all starting the same host.
  launcher_sites = var.launcher == null ? {} : var.launcher.sites
  launcher_count = var.launcher == null ? 0 : 1
  # The site may call only backends named *-<its environment>-* (the-lobby's site boundary), so each name carries the environment it serves.
  launcher_names    = { for environment in keys(local.launcher_sites) : environment => "${var.resource_prefix}-${environment}-launcher" }
  launcher_arns     = { for environment, name in local.launcher_names : environment => "arn:aws:lambda:${var.region}:${var.account_id}:function:${name}" }
  launcher_host_arn = "arn:aws:ec2:${var.region}:${var.account_id}:instance/${try(aws_instance.host[0].id, "absent")}"
  # The start record and the Cloudflare token belong to the host, not to a site, so every function shares them:
  # a start from one site shows as starting on the others.
  launcher_state_name      = "/${var.resource_prefix}/launcher/state"
  launcher_state_arn       = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${local.launcher_state_name}"
  launcher_token_name      = try(coalesce(var.launcher.cloudflare_token_parameter, "/${var.resource_prefix}/launcher/cloudflare-token"), null)
  launcher_token_arn       = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${coalesce(local.launcher_token_name, "/absent")}"
  launcher_log_group_names = { for environment, name in local.launcher_names : environment => "/aws/lambda/${name}" }
}

# The deployment that existed before launchers were per environment served dev as instance [0]. These move
# it in place of replacing it; delete them once every deployment has applied this version.
moved {
  from = aws_iam_role.launcher[0]
  to   = aws_iam_role.launcher["dev"]
}

moved {
  from = aws_iam_role_policy.launcher[0]
  to   = aws_iam_role_policy.launcher["dev"]
}

moved {
  from = aws_cloudwatch_log_group.launcher[0]
  to   = aws_cloudwatch_log_group.launcher["dev"]
}

moved {
  from = aws_lambda_function.launcher[0]
  to   = aws_lambda_function.launcher["dev"]
}

moved {
  from = aws_lambda_function_event_invoke_config.launcher[0]
  to   = aws_lambda_function_event_invoke_config.launcher["dev"]
}

moved {
  from = aws_lambda_function_url.launcher[0]
  to   = aws_lambda_function_url.launcher["dev"]
}

moved {
  from = aws_lambda_permission.launcher_url[0]
  to   = aws_lambda_permission.launcher_url["dev"]
}

moved {
  from = aws_lambda_permission.launcher_invoke[0]
  to   = aws_lambda_permission.launcher_invoke["dev"]
}

moved {
  from = aws_sns_topic.launcher_alerts[0]
  to   = aws_sns_topic.launcher_alerts["dev"]
}

moved {
  from = aws_sns_topic_subscription.launcher_alerts[0]
  to   = aws_sns_topic_subscription.launcher_alerts["dev"]
}

moved {
  from = aws_cloudwatch_metric_alarm.launcher_failing[0]
  to   = aws_cloudwatch_metric_alarm.launcher_failing["dev"]
}

resource "aws_iam_role" "launcher" {
  for_each = local.launcher_sites
  name     = local.launcher_names[each.key]
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
  for_each = local.launcher_sites
  name     = "launcher"
  role     = aws_iam_role.launcher[each.key].id
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
        Resource = [local.launcher_arns[each.key]]
      },
      {
        Sid      = "OwnLogsOnly"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:${var.region}:${var.account_id}:log-group:${local.launcher_log_group_names[each.key]}:*"]
      },
      ], var.idle_stop == null ? [] : [{
        Sid      = "ReadIdleStopProgress"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [local.idle_stop_state_arn]
    }])
  })
}

# Shared by every site's function: they start the same host, so they share one start record.
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
  for_each          = local.launcher_sites
  name              = local.launcher_log_group_names[each.key]
  retention_in_days = 30
}

resource "aws_lambda_function" "launcher" {
  for_each         = local.launcher_sites
  function_name    = local.launcher_names[each.key]
  description      = "Answers The Lobby's Minecraft Launcher (${each.key} site) and starts the family server."
  role             = aws_iam_role.launcher[each.key].arn
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
    log_group  = aws_cloudwatch_log_group.launcher[each.key].name
  }
  depends_on = [aws_iam_role_policy.launcher]
}

# A lost follow-up is reported failed after its deadline and can be started again, so it is never retried
# late into a session somebody else has already handled.
resource "aws_lambda_function_event_invoke_config" "launcher" {
  for_each                     = local.launcher_sites
  function_name                = aws_lambda_function.launcher[each.key].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_lambda_function_url" "launcher" {
  for_each           = local.launcher_sites
  function_name      = aws_lambda_function.launcher[each.key].function_name
  authorization_type = "AWS_IAM"
}

# Only this environment's site role may call the address (the-lobby ADR 0006). The second statement is the
# lambda:InvokeFunction grant AWS also requires for a Function URL, limited to calls through the URL.
resource "aws_lambda_permission" "launcher_url" {
  for_each               = local.launcher_sites
  statement_id           = "AllowSiteThroughFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.launcher[each.key].function_name
  principal              = each.value
  function_url_auth_type = "AWS_IAM"
}

resource "aws_lambda_permission" "launcher_invoke" {
  for_each                 = local.launcher_sites
  statement_id             = "AllowSiteInvokeThroughFunctionUrl"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.launcher[each.key].function_name
  principal                = each.value
  invoked_via_function_url = true
}

resource "aws_sns_topic" "launcher_alerts" {
  for_each = local.launcher_sites
  name     = "${local.launcher_names[each.key]}-alerts"
}

resource "aws_sns_topic_subscription" "launcher_alerts" {
  for_each  = local.launcher_sites
  topic_arn = aws_sns_topic.launcher_alerts[each.key].arn
  protocol  = "email"
  endpoint  = var.budget_notification_email
}

# One alarm for every way a start can go wrong without a Member noticing at once: a failed run (Lambda's
# errors), a follow-up that aged out before it ran (dropped async events), and a start request that failed or
# could not hand off its follow-up, which the function counts itself because the site still gets an answer.
resource "aws_cloudwatch_metric_alarm" "launcher_failing" {
  for_each            = local.launcher_sites
  alarm_name          = "${local.launcher_names[each.key]}-failing"
  alarm_description   = "A start from The Lobby failed, lost its follow-up, or could not update DNS. See docs/operations/launcher.md."
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.launcher_alerts[each.key].arn]
  ok_actions          = [aws_sns_topic.launcher_alerts[each.key].arn]

  metric_query {
    id          = "failures"
    expression  = "FILL(errors, 0) + FILL(dropped, 0) + FILL(starts, 0)"
    label       = "Launcher failures"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/Lambda"
      metric_name = "Errors"
      dimensions  = { FunctionName = aws_lambda_function.launcher[each.key].function_name }
      period      = 300
      stat        = "Sum"
    }
  }
  metric_query {
    id = "dropped"
    metric {
      namespace   = "AWS/Lambda"
      metric_name = "AsyncEventsDropped"
      dimensions  = { FunctionName = aws_lambda_function.launcher[each.key].function_name }
      period      = 300
      stat        = "Sum"
    }
  }
  metric_query {
    id = "starts"
    metric {
      namespace   = "Minecraft/Launcher"
      metric_name = "StartFailures"
      dimensions  = { FunctionName = aws_lambda_function.launcher[each.key].function_name }
      period      = 300
      stat        = "Sum"
    }
  }
}
