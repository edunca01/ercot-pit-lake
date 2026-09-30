# 0001. Record architecture decisions

Date: 2026-09-03
Status: accepted

## Context

This project is revisited months apart, and anyone who deploys their own copy needs to know why
it is built the way it is. The reasoning behind stack and schema choices is lost unless it is
written down next to the code.

## Decision

Use lightweight ADRs in `docs/adr/`, one per decision, numbered sequentially. They record what
was decided, what was weighed and what it costs. `docs/CONTRACT.md` states the interface those
decisions produce.

## Consequences

- A change that constrains future work links an ADR.
- ADRs are immutable once accepted. Write a new ADR that supersedes the old one; don't edit it.
