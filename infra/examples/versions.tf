terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # No backend: this is an example root. A real deployment copies it, adds an S3 backend
  # (use_lockfile = true) and keeps its values out of version control.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = var.default_tags
  }
}
