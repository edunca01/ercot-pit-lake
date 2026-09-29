# samples/

ERCOT fixtures the transform tests run against, one API page and one archive posting per
product. ERCOT public market information, redistributed under ERCOT's terms of use.

| Path | Produced by | What it holds |
|---|---|---|
| `api/<product>.json` | `make samples` | rows from a recent delivery window of the report endpoint |
| `archive/<product>.csv` | `make archives` | one posting's CSV, its lines byte for byte as ERCOT wrote them |
| `archive/<product>/*.zip` | `make archives` | the full posting (gitignored; re-trim with `--from`) |
| `manifest.json` | both | where each sample came from: posting time, document, query |

## Which rows are kept

ERCOT lists resource nodes before hubs and load zones, so the first N rows of a price report
contain none of the settlement points anyone reads. Samples are trimmed by a rule instead
(`scripts/samples.py`), applied the same way to both formats:

- the first two intervals, in source order (an interval is the product's declared time
  columns plus its repeated-hour flag);
- price tables: every `HB_` and `LZ_` row of those intervals, of every type (so load zones
  appear as both `LZ` and `LZEW`), plus the first five other settlement points of each;
- every other table: every row of those intervals (all AS types, all series, every forecast
  model, so filters such as the in-use model have rows to drop).

An archive sample is always exactly one posting, because that is the unit ingestion reads.
RT and SCED postings hold a single interval, so their multi-interval coverage comes from the
API sample.

## Changing a sample

Never edit these by hand. Re-run the script and review the diff: a changed header is ERCOT
changing the report, and that is a declaration change (and a schema version bump if curated
columns change), not a fixture update.

```
make samples archives PRODUCTS=np6-905-cd              # live, needs ERCOT_* credentials
uv run python -m scripts.fetch_archives --products np6-905-cd --from samples/archive/np6-905-cd/X.zip
```
