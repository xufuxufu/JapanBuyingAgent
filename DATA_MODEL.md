# DATA_MODEL.md

Last updated: 2026-08-27. Source of truth: `app/models.py`, migrations, and the current SQLite schema.

## Core Product Tables

| Table | Purpose | Key Fields and Constraints |
|---|---|---|
| `products` | Local product master | `internal_sku` unique; `jan` unique where not null; `qinsi_product_code` unique where not null; names, image fields, QinSi master fields, spec fields, price fields, status/source/origin |
| `product_barcodes` | Additional scan barcode links | `product_id`, `barcode`, `source_system`, `is_primary`; unique per product/barcode; barcode indexed but can be shared across products |
| `product_aliases` | Confirmed aliases | unique `product_id` + `normalized_alias` |
| `product_operation_logs` | Product audit | product/internal SKU, action, actor, before/after JSON |
| `product_placeholder_cleanup_logs` | Cleanup audit | old/new product ids, JAN, action, counts |
| `qinsi_product_mappings` | Confirmed QinSi code mapping | unique `qinsi_product_code`, `product_id` |

## Receipt Tables

| Table | Purpose | Key Fields and Constraints |
|---|---|---|
| `receipt_batches` | Upload batch | `batch_no` unique, statuses, image/GPT/product/QinSi stages |
| `receipt_images` | Original/processed image provenance | unique batch/page, unique recognition/stored filenames, hash fields, duplicate status |
| `zip_package_jobs` / `zip_package_items` | Multi-batch recognition ZIP audit | unique selection/job keys; unique job/image |
| `ai_recognition_runs` | Manual ChatGPT import run | provider/model/prompt/raw/normalized JSON/status |
| `receipts` | One recognized receipt | source image, store raw/match fields, totals, confirmation/review/duplicate status |
| `receipt_items` | Atomic receipt line | unique receipt/line, raw and recognized names, JAN candidate, product link, quantity, price, discount, tax, line total, source image |
| `duplicate_detection_logs` | Duplicate audit | entity ids, algorithm version, evidence, score, decision |

## Purchase and QinSi Export Tables

| Table | Purpose | Key Fields and Constraints |
|---|---|---|
| `purchase_batches` | Confirmed purchase event | one receipt relation, GPT batch, store/operator/time, local/QinSi locations, status |
| `purchase_batch_items` | Purchase fact lines | product and receipt item links, quantity, unit price, discount, actual line amount, locations |
| `qinsi_purchase_export_jobs` | QinSi purchase export job | selected batches, status/result counts, download/confirmation audit |
| `qinsi_purchase_export_lines` | Exported purchase lines | unique export job + purchase item |
| `qinsi_purchase_export_line_sources` | Source links | unique active source relation for export line/source |
| `qinsi_export_jobs` | QinSi new-product export job | status, counts, file content, confirmation |
| `qinsi_export_lines` | Product export lines | unique job + product |
| `qinsi_export_line_sources` | Receipt sources for product export | partial unique active receipt item |

## QinSi Import and Conflict Tables

| Table | Purpose | Key Fields and Constraints |
|---|---|---|
| `qinsi_import_batches` | QinSi import preview/confirm job | file hash unique, source system, parse version, counts, summary/error/file content |
| `qinsi_goods_import_rows` | Parsed QinSi rows | unique batch/excel row, QinSi product code, barcode, raw/parsed/warnings/errors/conflict JSON, product link |
| `qinsi_master_values` | QinSi master lookup values | unique source/type/name, active flag |
| `qinsi_conflict_resolutions` | Reusable conflict decisions | unique `conflict_key`, barcode, QinSi product codes JSON, resolution type, action, note, auto_apply |

## Price and Enrichment Tables

| Table | Purpose | Key Fields and Constraints |
|---|---|---|
| `marketplaces` | Provider identity | unique code |
| `price_search_runs` | One lookup run | product/JAN/status/cache/provider summary |
| `product_offers` | Timestamped offers | search run, marketplace, product/JAN, prices, stock, match trust, raw JSON |
| `price_provider_attempts` | Provider attempt audit | status in success/empty/timeout/error/unconfigured/manual_only |
| `price_lookup_histories` | User lookup history | run/product/JAN/current store price/cache/source |
| `product_enrichment_tasks` | Product completion task | JAN/product/status/trigger and warning/error data |
| `product_enrichment_candidates` | Candidate online data | platform/source URL/name/price/image/spec/brand/raw/warnings |
| `product_enrichment_sources` | Task source links | unique task/source type/source id |
| `product_translation_cache` | Translation cache | unique JAN + Japanese-name hash |
| `enrichment_audit_logs` | Enrichment audit | action/source/result payloads |

## Field Purchase and Runtime Job Tables

| Table | Purpose |
|---|---|
| `field_purchase_batches` | Mobile purchasing sessions |
| `field_purchase_items` | Captured field-purchase facts and review status |
| `tag_evidence` | Uploaded tag/photo evidence for field items |
| `field_purchase_sync_requests` | Idempotent offline sync requests |
| `durable_background_jobs` | Retryable background jobs such as image localization |
| `platform_provider_states` / `platform_lookup_results` | Provider diagnostics and lookup result state |

## Stores, Locations, Inventory, Restock, Watches

| Table | Purpose |
|---|---|
| `store_brands`, `stores`, `store_aliases` | Store master data and matching aliases |
| `locations` | Physical locations and QinSi warehouses |
| `qinsi_inventory_snapshots`, `qinsi_inventory_snapshot_lines` | Timestamped QinSi inventory observations |
| `inventory_transactions` | Reserved/legacy local transaction table; do not treat as QinSi current stock |
| `restock_lists`, `restock_list_items` | Store-specific restock workflows |
| `product_watch_configs`, `product_watch_recommendations`, `product_watch_snapshots`, `product_watch_notifications` | Price watch rules, recommendations, checks, and notifications |
| `monitor_scheduler_states`, `price_watch_rules`, `price_alerts` | Monitoring scheduler and earlier price alert structures |
