# Data architecture reservations

Deprecated historical note, last reviewed 2026-08-27. This file preserves useful early architecture rationale, but it contains outdated "future phase" statements. Use `../ARCHITECTURE.md`, `../DATA_MODEL.md`, and `../BUSINESS_RULES.md` as the current entry points.

## Ownership boundary

- QinSi owns current inventory balances. This local application never mirrors or overwrites QinSi's current inventory balance.
- The local database owns product reference data, immutable purchase facts, receipt/image provenance, and QinSi export history.
- Excel is an import/export artifact, not live storage. This phase does not generate a QinSi workbook or formally import into QinSi.
- Marketplace prices are timestamped observations. Offers never overwrite `products.purchase_price` or receipt purchase prices.

## Receipt provenance

`receipt_batches → receipt_images → receipts → receipt_items` retains batch and line-level source relationships. Each image has a stable `recognition_filename`; schema 1.1 batch imports resolve `source_file` plus `source_page_no` only against included `zip_package_items` in the current GPT task and store the resulting `source_image_id`. Exact matching is mandatory and cross-task names are rejected. Schema 1.0 remains available only for a single-image task.

Receipt items remain atomic purchase facts. Repeated purchases of the same product, including multiple purchases on the same day, are not merged in the receipt layer. A product may link to many receipt items across many receipts.

## Duplicate detection and ZIP audit

Image deduplication is evaluated before a new original is written. Exact byte SHA-256, an EXIF-normalized pixel hash, or an extremely close visual signature plus matching aspect/content evidence may auto-skip an upload. General visual similarity is retained as a new image and deferred to business evidence. Historical visual hashes are filled incrementally with `scripts/backfill_image_hashes.py`; migrations never recalculate image pixels.

After each accepted manual JSON import, receipt facts are compared across batches. Receipt number + normalized store, the same source image, highly similar source + store + paid total, or a complete store/time/amount/item match can auto-link a duplicate. Different receipt numbers, clearly different purchase times, different item/quantity summaries, and same-store/same-amount images with different content remain distinct. Conflicting image and business evidence becomes `review_required`. Duplicate receipts retain their images, receipt items and every AI run; no physical merge occurs.

Master selection prefers reviewed data, then completeness, source-image resolution and earliest upload. `duplicate_detection_logs` records algorithm version, evidence, score and decision so a later version can recalculate results.

`zip_package_jobs` and `zip_package_items` audit reproducible multi-batch recognition ZIP downloads. Items are ordered by batch time and page number, use only database `recognition_filename` values, and record exclusions. Auto-duplicate receipts/images are excluded; downloading never mutates image files. The same effective selection reuses its job and increments first/latest download timestamps and count.

## QinSi export reservation

- `qinsi_export_jobs`: one future export attempt, status `pending / exported / confirmed / failed`.
- `qinsi_export_lines`: product-level quantity aggregated only for that export job.
- `qinsi_export_line_sources`: many-to-one links from original receipt items to an export line, including contributed quantity.
- A partial unique index on active source links prevents one `receipt_item` from entering two effective export jobs. A failed/cancelled future workflow must deactivate its source links before retrying.

No current inventory balance is stored and no Excel generation is implemented in this phase.

## JAN price snapshot reservation

- `marketplaces`: marketplace identity and base URL.
- `price_search_runs`: a future search attempt tied to `products.id` with a copied JAN query value.
- `product_offers`: timestamped offer snapshots with integer-JPY item, shipping and total prices; marketplace, seller, URL, stock, single/bundle type, new/used condition, match status and raw payload.
- `price_watch_rules` and `price_alerts`: inactive architecture for future thresholds and alert records.

No network request, crawler, scheduler or notification is implemented. `products.jan` stays the product identifier; an offer is only a time snapshot and never the product's unique price fact.
