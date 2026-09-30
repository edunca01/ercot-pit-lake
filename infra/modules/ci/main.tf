# CI layer: GitHub Actions authenticates with OIDC, no long-lived keys. Two roles:
#   <prefix>-github-plan    pull requests      read everything, write nothing (plan -lock=false)
#   <prefix>-github-deploy  the production     push the image, apply Terraform
#                           environment

locals {
  oidc_host = "token.actions.githubusercontent.com"
  # The deploy role can only touch IAM entities named with the project prefix.
  iam_scope = [
    "arn:aws:iam::${var.account_id}:role/${var.name_prefix}-*",
    "arn:aws:iam::${var.account_id}:policy/${var.name_prefix}-*",
  ]
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.oidc_host}"
  client_id_list = ["sts.amazonaws.com"]
  # AWS validates GitHub's certificate chain itself; the thumbprint is required by the API
  # but no longer load-bearing.
  thumbprint_list = ["ffffffffffffffffffffffffffffffffffffffff"]
}

data "aws_iam_policy_document" "assume_plan" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "${local.oidc_host}:sub"
      values = flatten([
        for p in var.github_sub_prefixes : ["${p}:pull_request", "${p}:ref:refs/heads/*"]
      ])
    }
  }
}

data "aws_iam_policy_document" "assume_deploy" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }
    # For a job in a GitHub environment the subject is `...:environment:<name>`, not the
    # branch form, so the environment's branch rule is what limits deploys to main.
    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:sub"
      values   = [for p in var.github_sub_prefixes : "${p}:environment:${var.deploy_environment}"]
    }
  }
}

# -- Plan role ----------------------------------------------------------------------------------

resource "aws_iam_role" "plan" {
  name                 = "${var.name_prefix}-github-plan"
  assume_role_policy   = data.aws_iam_policy_document.assume_plan.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# ReadOnlyAccess covers state reads and every describe/list a plan needs. Explicitly deny the
# one read that must never happen in CI.
data "aws_iam_policy_document" "no_secret_values" {
  statement {
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "plan_deny" {
  name   = "no-secret-values"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.no_secret_values.json
}

# -- Deploy role --------------------------------------------------------------------------------

resource "aws_iam_role" "deploy" {
  name                 = "${var.name_prefix}-github-deploy"
  assume_role_policy   = data.aws_iam_policy_document.assume_deploy.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "deploy_readonly" {
  role       = aws_iam_role.deploy.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

data "aws_iam_policy_document" "deploy" {
  # Terraform state, locked with S3-native lock files beside it
  statement {
    sid       = "State"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
    resources = ["arn:aws:s3:::${var.tfstate_bucket}", "arn:aws:s3:::${var.tfstate_bucket}/*"]
  }

  # Image push
  statement {
    sid       = "EcrLogin"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability", "ecr:CompleteLayerUpload", "ecr:InitiateLayerUpload",
      "ecr:PutImage", "ecr:UploadLayerPart", "ecr:BatchDeleteImage",
      "ecr:PutLifecyclePolicy", "ecr:DeleteLifecyclePolicy", "ecr:TagResource", "ecr:UntagResource",
      "ecr:PutImageScanningConfiguration", "ecr:PutImageTagMutability",
      "ecr:SetRepositoryPolicy", "ecr:DeleteRepositoryPolicy",
    ]
    resources = var.ecr_repository_arns
  }

  # Everything Terraform manages in this project, resource-scoped where the service allows.
  statement {
    sid       = "Lake"
    actions   = ["s3:*"]
    resources = [var.lake_bucket_arn, "${var.lake_bucket_arn}/*"]
  }
  statement {
    sid       = "StateTable"
    actions   = ["dynamodb:*"]
    resources = [var.state_table_arn]
  }
  statement {
    sid = "SecretContainer"
    actions = [
      "secretsmanager:UpdateSecret", "secretsmanager:TagResource", "secretsmanager:UntagResource",
      "secretsmanager:DeleteSecret", "secretsmanager:RestoreSecret",
    ]
    resources = [var.secret_arn]
  }
  statement {
    sid = "PublishedParameters"
    actions = [
      "ssm:PutParameter", "ssm:DeleteParameter", "ssm:AddTagsToResource",
      "ssm:RemoveTagsFromResource",
    ]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:parameter/${var.ssm_prefix}/*"]
  }
  statement {
    sid       = "Lambda"
    actions   = ["lambda:*"]
    resources = ["arn:aws:lambda:${var.region}:${var.account_id}:function:${var.name_prefix}-*"]
  }
  statement {
    sid     = "Scheduler"
    actions = ["scheduler:*"]
    resources = [
      "arn:aws:scheduler:${var.region}:${var.account_id}:schedule/${var.schedule_group}/*",
      "arn:aws:scheduler:${var.region}:${var.account_id}:schedule-group/${var.schedule_group}",
    ]
  }
  statement {
    sid = "Observability"
    actions = [
      "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:TagResource",
      "logs:UntagResource", "logs:PutMetricFilter", "logs:DeleteMetricFilter",
      "cloudwatch:PutMetricAlarm", "cloudwatch:DeleteAlarms", "cloudwatch:TagResource",
      "cloudwatch:UntagResource",
      "sns:CreateTopic", "sns:DeleteTopic", "sns:SetTopicAttributes", "sns:Subscribe",
      "sns:Unsubscribe", "sns:TagResource", "sns:UntagResource",
      "chatbot:*",
    ]
    resources = ["*"]
  }

  # IAM: only the project's own roles and policies, plus handing them to the services that
  # run them.
  statement {
    sid = "ProjectIam"
    actions = [
      "iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateRoleDescription",
      "iam:UpdateAssumeRolePolicy", "iam:TagRole", "iam:UntagRole",
      "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
      "iam:CreatePolicy", "iam:DeletePolicy", "iam:CreatePolicyVersion", "iam:DeletePolicyVersion",
      "iam:TagPolicy", "iam:UntagPolicy",
    ]
    resources = local.iam_scope
  }
  statement {
    sid       = "PassProjectRoles"
    actions   = ["iam:PassRole"]
    resources = ["arn:aws:iam::${var.account_id}:role/${var.name_prefix}-*"]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com", "scheduler.amazonaws.com", "chatbot.amazonaws.com"]
    }
  }
  statement {
    sid = "OidcProvider"
    actions = [
      "iam:TagOpenIDConnectProvider", "iam:UntagOpenIDConnectProvider",
      "iam:UpdateOpenIDConnectProviderThumbprint",
    ]
    resources = [aws_iam_openid_connect_provider.github.arn]
  }
  statement {
    sid       = "NoSecretValues"
    effect    = "Deny"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.name_prefix}-github-deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
