# BUSINESS_RULES.md

Last updated: 2026-08-27. If this file conflicts with current code, migrations, tests, or database schema, fix the document after verifying the code.

## Product, JAN, and Barcode

- `Product.jan` stays unique for non-null values through `uq_products_jan_not_null`.
- Blank JAN is allowed.
- QinSi 货号 is `qinsi_product_code`; it is unique when present and never automatically means JAN.
- JAN/barcode values are text. Preserve leading zeroes and reject unsafe numeric conversion.
- Valid JAN in QinSi imports is selected in this priority: 单品条码, 商品条码, then 货号 only if it passes JAN validation.
- A valid-looking QinSi 货号 can still be a QinSi code. Do not use it as a scan JAN unless current parser/matching rules prove it is the intended JAN.
- Shared barcodes are allowed in `product_barcodes`; `ProductBarcode.barcode` is indexed but not globally unique.
- Unique scan match may proceed directly. Ambiguous scan match must show candidate products for human choice.

## QinSi Product Master

QinSi is the formal product master authority. Formal QinSi list imports are recognized by headers, not by a single required sheet name. Current parser supports QinSi product-list columns such as:

`商品名称`, `商品规格`, `货号`, `商品条码`, `单品条码`, `型号规格`, `图片链接`, `品牌`, `分类`, `单位`, `采购价`, `销售价`, `最低销售价`, `保质期`, `产地`, `适用年龄`, `排序`, `状态`, `库存预警下限`, `库存预警上限`, `备注`.

On formal sync, QinSi can overwrite local product-master fields including name and product image. This includes clearing previous local/manual image state when QinSi supplies an authoritative image URL.

Never overwrite purchase facts:

- `ReceiptItem.raw_name`, `recognized_name`, quantity, unit price, discount, line total;
- `PurchaseBatch` and `PurchaseBatchItem` quantities, prices, discounts, source links, and locations.

## QinSi Conflicts

Conflict resolution records live in `qinsi_conflict_resolutions` and use stable `conflict_key` values based on barcode, QinSi product codes, and resolution type, not Excel row numbers.

Current resolution types:

- `shared_barcode_variant`
- `qinsi_legacy_data`
- `code_barcode_overlap`
- `true_duplicate`
- `wrong_barcode`
- `other`

Rows matching `auto_apply=true` historical rules can be resolved automatically in later previews. Shared-barcode variants should keep multiple products and attach the barcode relation to each product rather than merging.

## Receipt Recognition

Receipt OCR/import uses schema version `1.1`.

- `source_file` is copied exactly from the recognition task.
- `source_page_no` must match the page.
- Each item repeats its receipt source fields.
- Unknown values are `null`.
- Do not infer JAN from product name, QinSi code, store code, or any non-JAN identifier.
- Preserve receipt abbreviations in `raw_name`.
- Keep quantity, unit price, discount, tax, and line total separate.
- Receipt-level discounts are not automatically averaged into item lines.
- Large image sets should be split into multiple recognition batches and merged through the app flow; do not rely on one huge GPT JSON response.

## Receipt to PurchaseBatch

One confirmed `Receipt` creates one `PurchaseBatch`. A recognition batch may contain multiple receipts and therefore multiple purchase batches. Do not merge multiple receipts into one purchase batch unless a future explicit product decision and migration says so.

Supported behavior includes confirmed receipt flow, confirmed item correction, JAN normalize/validate/rematch, bulk-confirm normal receipts, manual unmarking of duplicate receipts, and backfilling the missing purchase batch after a receipt is unmarked.

## Product Completion and Enrichment

New products must not be blocked from QinSi new-product export only because Chinese name, image, brand, spec, or online result is missing. The practical minimum is valid identity plus a usable name.

Official-page enrichment parses in this order: JSON-LD Product, OG/meta, H1, title. It can save name, price, image, spec, brand/manufacturer, and source URL. Unknown price must not display or persist as `¥0`.

## Price Check

Price-check is designed for field use and favors live lookup. Product-list "查价" links default to `refresh=1`. If live lookup has no result, saved historical online sources should still be visible when available.

The main scan page must stay focused: Rakuten export-IP diagnostics are not shown on the primary scan page; provider diagnostics belong in platform config or monitor pages.

## Images

Internal display images and QinSi product-master images are related but separate concerns.

- QinSi product-master sync may replace local/manual image state with QinSi image URL.
- QinSi new-product export may leave image column blank when only a local file exists.
- Localhost, local path, and private-only image references are not valid QinSi public image URLs.
- Image localization may cache public images locally for display/offline use, but that cache is runtime data.

## QinSi Purchase Confirmation

QinSi purchase export confirmation supports `all_success`, `partial_failure`, and `all_failed`. Confirmation forms use hidden result values rather than relying only on disabled submit buttons.
