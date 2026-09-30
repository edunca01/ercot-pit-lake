# 0004. RT ancillary-service price source

Date: 2026-09-04
Status: accepted

## Context

The lake needs real-time ancillary-service clearing prices (15-min, product `np6-331-cd`). It
was unclear whether this product is on the Public API. ERCOT's documentation names archive
`NP6-796-ER` as the alternative.

## Findings (verified 2026-09-04 against the live API)

- `np6-331-cd` **is live** on the Public API at `/np6-331-cd/rt_clear_price_cap`
  (report type 24898, "Real-Time Clearing Prices for Capacity by 15-Minute Settlement Interval").
  - First run 2025-12-05, when RTC+B went live.
  - Posted every 15 minutes.
  - Archive retention 2555 days.
- The value is the time-weighted average, over the interval, of the RT MCPC plus the RT
  Reliability Deployment Price Adder, per AS type (REGUP, REGDN, RRS, ECRS, NSPIN).
- `NP6-796-ER` is not a weekly CSV. It is a yearly **xlsx** (`HIST_15Min_RTM_MCPC_<year>.xlsx`,
  ~2.8 MB), and the only source for RT MCPCs before 2025-12-05.
- For every product, the API field names differ from the archive CSV headers. Both shapes are
  captured under `samples/`, and each transform accepts both.

| Product | API fields | Archive CSV header |
|---|---|---|
| np4-190-cd | deliveryDate, hourEnding, settlementPoint, settlementPointPrice, DSTFlag | DeliveryDate, HourEnding, SettlementPoint, SettlementPointPrice, DSTFlag |
| np4-188-cd | deliveryDate, hourEnding, ancillaryType, MCPC, DSTFlag | DeliveryDate, HourEnding, AncillaryType, MCPC, DSTFlag |
| np6-905-cd | deliveryDate, deliveryHour, deliveryInterval, settlementPoint, settlementPointType, settlementPointPrice, DSTFlag | DeliveryDate, DeliveryHour, DeliveryInterval, SettlementPointName, SettlementPointType, SettlementPointPrice, DSTFlag |
| np6-331-cd | deliveryDate, deliveryHour, deliveryInt, repeatHourFlag, ASType, MCPC | DeliveryDate, DeliveryHour, DeliveryInterval, RepeatedHourFlag, ASType, MCPC |

## Decision

- `np6-331-cd` is a normal live 15-min product: `endpoint` set, `live: true`, schedule
  `cron(3/15 * * * ? *)`.
- `NP6-796-ER` stays in config as `fallback_archive_id`, for history only. RT MCPC history
  starts at 2025-12-05. Pre-RTC+B values are not comparable, and are not collected.

## Consequences

- No special "RT AS unavailable" handling is needed anywhere.
- The JSON row endpoints return rows with no posting timestamp. `posted_at` is required
  (ADR 0003), so ingestion reads archive documents instead (ADR 0005).
- ERCOT returns 429 readily. The client honours `Retry-After` and paces requests at ~2.5 s.
