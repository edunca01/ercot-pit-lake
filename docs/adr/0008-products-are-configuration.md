# 0008. Products are configuration

Date: 2026-09-24
Status: accepted

## Context

The lake started with nine ERCOT reports. ERCOT publishes hundreds, and most have one of three
shapes that already have a curated table:

- a price per settlement point (`spp`)
- a price per ancillary-service type (`mcpc`)
- numeric columns per interval (`series`)

Until now, every product needed its own Python transform, even when that transform only renamed
columns and parsed ERCOT's time format. Collecting more data should not mean writing more code.

## Decision

- `config.yaml` `products` is the list of what the lake collects: every entry is scheduled,
  freshness-checked and `live` in `manifests/_catalog.json`. There is no enable/disable
  switch, because a switch would let the collected set drift from what the code declares.
  - Removing an entry stops collecting that product.
  - A removed product that already has data stays in the catalog with `live: false`.
- An entry for an existing table family can declare its transform instead of coding it:
  - the source columns, both the API field names and the archive CSV headers
  - how ERCOT expresses time: delivery date + hour ending, + interval, or a SCED timestamp
  - a mapping onto the contract columns
  One generic transform per family runs these declarations.
- The declared columns are exhaustive. Any difference in a posting raises `SchemaDriftError`,
  exactly as a hand-written transform does (ADR 0005).
- A product that needs more than renaming and time parsing names a Python transform
  (`transform: ingest.transforms.<module>`) instead. This hook is built when the first such
  product arrives; all built-in products fit declarations.
- Every product, declared or coded, has a committed sample. One parametrized test runs every
  catalog entry against its sample and checks the output against the contract schema.
- A new table family is still a contract change (`docs/CONTRACT.md` §1).

## Consequences

- Adding a report with a known shape means one YAML entry and one sample.
- The declaration language stays small: rename, time style, melt. Anything more is code, so the
  YAML never becomes a programming language.
- The existing hand-written transforms move to declarations where they fit. The move is
  accepted only if their curated output stays byte-identical apart from `ingested_at`.
- Each product added to the lake is a contract minor release (`docs/CONTRACT.md` §1).
