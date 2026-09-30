# 0007. The lake is its own repository: public code, private deployment

Date: 2026-09-24
Status: accepted

## Context

Consumers of the lake need its layout, schemas and read rule, not the pipeline's internals.
The code should be something anyone can read, run offline on the committed samples, and deploy
into their own AWS account.

A running deployment also carries values that should never be public: the AWS account, the
Terraform state location, the OIDC trust, alert destinations and the data subscription.

## Decision

- The lake is its own repository. Consumers depend on `docs/CONTRACT.md` and on the `ercot-lake`
  package at a release tag, never on this repository's modules.
- This repository is public and holds the code and nothing about one deployment:
  - the pipeline and the read library
  - reusable Terraform modules, plus an example root module that validates without a backend
  - CI that needs no secrets and no cloud account
- A separate private repository holds the live deployment:
  - the root module, with its backend, variables and state moves
  - the deploy workflow (GitHub OIDC)
  - operational runbooks
  It builds the Lambda image from a pinned commit of this repository and applies that.

## Consequences

- Nothing in this repository can deploy, so a public pull request or a fork can never reach
  the production account.
- Going to production is an explicit step: the private repository moves its pin to a new
  commit or tag. Plans and applies run there.
- Anyone can deploy their own copy. They write a root module like `infra/examples/` with their
  own backend and variables.
- There are two repositories to keep in step. The contract version (`CONTRACT_VERSION`,
  published to SSM) tells consumers which release is running.
