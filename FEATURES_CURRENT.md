# FEATURES_CURRENT.md

Last updated: 2026-08-27. This is implemented-feature inventory, not a roadmap.

## Main Pages and Workflows

- Home/more/task pages expose operational entry points for receipts, products, field purchase, QinSi, price, watches, notifications, inventory snapshots, stores, restock, analytics, and platform config.
- Health endpoint: `GET /health`.
- Platform config supports provider status/testing and keeps Rakuten diagnostics outside the main scanner.

## Receipts

- Upload camera/album receipt images, preserve originals, create processed recognition images, rotate/reprocess/select recognition source.
- Build single-batch and multi-batch recognition ZIP jobs.
- Manual ChatGPT schema `1.1` JSON import with exact `source_file` and `source_page_no` matching.
- Review receipt headers, stores, items, ignored/deleted lines, product binding, new-product creation, matching, and final confirmation.
- Bulk-confirm normal receipts and manually unmark duplicate receipts.

## Products

- Product list and detail pages with mobile cards, image handling, editing, archive/restore/delete rules, inventory settings, generated Chinese names, photo completion, auto-image restore, and price-check links.
- API product create/update with stable `internal_sku`, nullable JAN, unique non-null JAN, and unique QinSi product code.
- Duplicate JAN governance/merge pages and placeholder-cleanup flow.
- Main image routes for local and localized images.

## Field Purchase

- Mobile field-purchase scanner with local JAN lookup, ambiguous-candidate handling, new draft creation, tag-evidence image capture, offline/durable sync request handling, batch store updates, and manual review/confirmation.
- Existing product scans and new product drafts preserve capture facts and idempotency keys.
- Shared barcode scans return candidates rather than randomly choosing.

## QinSi Product Import and Export

- Formal QinSi product-list import by header recognition, including Sheet1 and other compatible sheet names.
- Preview counts for new/update/unchanged/skipped/conflict/error/warning rows.
- Conflict resolution with reusable historical rules and audit workbook export.
- QinSi product-master sync can update authoritative product names/images and master fields while preserving purchase facts.
- New-product QinSi export pages/download/confirmation/cancel-confirmation exist.

## Purchase Batches and QinSi Purchase Export

- Confirmed receipts generate purchase batches and purchase items.
- Purchase batch list/detail pages show source receipt links, stores, operators, locations, and metadata edits.
- QinSi purchase export job creation, download, confirmation, cancel confirmation, retry, and merged export entry points exist.
- Confirmation supports all-success, partial-failure, and all-failed outcomes.

## Price, Enrichment, and Monitoring

- JAN price check with refresh default from product links and result pages with provider attempts, trusted offers, historical lookup, and current store price comparison.
- Rakuten Item Search default, Yahoo provider, local provider, manual/search-only handling, provider coalescing, 403/429/empty/timeout states, and web fallback parsing.
- Official-page parser extracts product name, image, price, spec, brand/manufacturer, and source URL.
- Enrichment tasks/candidates can retry, accept, bind existing products, reject candidates, and batch-accept.
- Product watches, target-price recommendations, due checks, notifications, read/archive actions, and monitor status pages are implemented.

## Stores, Locations, Analytics, Restock

- Store brands, stores, aliases, product-store purchase history, and store detail pages.
- Location master includes physical locations and QinSi warehouses.
- Purchase analytics pages summarize trends, stores, products, inventory distribution, and procurement details.
- Restock lists support store-specific candidates, list status/copy, item updates, temporary purchased markers, and formal receipt back-links.

## QinSi Inventory Snapshots

- Upload/list/detail/review/download QinSi inventory snapshots.
- Matching priority uses JAN/code/confirmed mapping; fuzzy names do not auto-match.
- Unknown warehouses are exceptions and are not silently created.
- Latest inventory view aggregates warehouses and marks stale data.
