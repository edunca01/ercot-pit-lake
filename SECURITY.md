# Security policy

## Reporting a vulnerability

Please report privately through GitHub: **Security → Report a vulnerability** on this
repository. Do not open a public issue, pull request or discussion.

Include what you found, how to reproduce it and what it affects. You will get an
acknowledgement within 7 days.

## Scope

- The pipeline code (`ingest/`), the read library (`lake/`), the Terraform (`infra/`) and the
  CI workflows.
- Anything that would let a consumer write to `raw/`, `curated/` or `manifests/`, or read `raw/`.
- A committed credential, account ID or other deployment identifier.

ERCOT's own API and data are out of scope; report those to ERCOT.

## Supported versions

The latest `lake-vX.Y.Z` release and `main`.
