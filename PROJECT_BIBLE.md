# PROJECT_BIBLE.md

Last updated: 2026-08-27. Facts in this file must follow current code, migrations, tests, and the live SQLite schema before older notes.

## Product Position

Japan Buying Agent (JBA) is a field purchasing assistant for Japanese buying work. Its core responsibilities are receipt and purchase facts, local product handling, JAN/barcode lookup, online price/enrichment support, and QinSi import/export files.

QinSi remains the official product, inventory, and sales system. JBA must not grow into a duplicate full QinSi inventory/sales system.

## Non-Negotiable Boundaries

- QinSi is authoritative for formal product master data and current inventory balances.
- JBA owns local operational facts: uploaded images, receipt recognition provenance, confirmed receipt lines, purchase batches, field-purchase captures, price snapshots, enrichment audit history, and QinSi import/export audit records.
- Purchase facts are immutable business evidence. Do not overwrite `ReceiptItem` raw names, prices, quantities, discounts, line totals, or `PurchaseBatchItem` facts during product sync.
- `Product.jan` is currently unique when not null. Do not remove that uniqueness again without a deliberate migration and test plan.
- QinSi product code (`qinsi_product_code`, QinSi 货号) is not JAN. A 13-digit product code must not be blindly treated as a JAN.
- Shared real-world barcodes can map to multiple QinSi variants. Store those relationships in `product_barcodes`; do not force product merges.
- Unknown data stays null. Do not invent JAN, price, image, brand, spec, store, or receipt fields from names.
- Do not write localhost paths, local filesystem paths, or private image paths into QinSi image-url columns.
- Do not commit SQLite databases, uploaded images, localized product images, secrets, or machine-specific absolute paths.

## QinSi Authority

Formal QinSi product-list import is treated as product-master sync. On sync, QinSi product name, image URL, QinSi code, barcodes, brand, category, unit, prices, status, notes, and structured master fields can update Product master data according to current import code.

This authority never reaches backward into receipt or purchase facts. Receipt lines and purchase-batch lines remain evidence of what was bought, where, when, for what price, and under what OCR/import provenance.

## Identity Model

- `internal_sku`: local stable product identity.
- `Product.jan`: validated JAN/GTIN scan identity; unique when present.
- `qinsi_product_code`: QinSi 货号; unique when present; not a JAN by default.
- `ProductBarcode.barcode`: additional scan barcode relation. The same barcode may point to multiple products when QinSi variants share a barcode.
- `ProductAlias`: confirmed aliases used by matching flows.

Scanning behavior must be deterministic: a unique hit opens that product; multiple candidates require human selection.

## Canonical Docs

- [AI_HANDOFF.md](AI_HANDOFF.md)
- [BUSINESS_RULES.md](BUSINESS_RULES.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [DATA_MODEL.md](DATA_MODEL.md)
- [FEATURES_CURRENT.md](FEATURES_CURRENT.md)
- [OPERATIONS.md](OPERATIONS.md)
- [KNOWN_ISSUES_AND_ROADMAP.md](KNOWN_ISSUES_AND_ROADMAP.md)
