# QinSi field mapping

This mapping is a field reference for future import/export work. No workbook row is imported in this MVP.

## Locked identifier mapping

| QinSi column | Local field | Rule |
|---|---|---|
| 条码 | `products.jan` | **条码 → jan**. Store as text, preserve leading zeroes, nullable; non-null values unique. |
| 货号（必填且唯一） | `products.qinsi_product_code` | **货号 → qinsi_product_code**. Store as text, nullable locally; non-null values unique. Never use as JAN. |

## Product mapping

| QinSi column | Local field | Conversion / validation |
|---|---|---|
| 名称（必填） | `products.name_cn` | Required for QinSi export. |
| 型号规格 | `products.model_spec` | Text. |
| 商品规格 | `products.specification` | Present only in the existing-product workbook. |
| 采购价 | `products.purchase_price` | Integer JPY; reject decimal or non-numeric input. |
| 销售价 | `products.sale_price` | Integer JPY. |
| 最低销售价 | `products.minimum_sale_price` | Integer JPY or null. |
| 商品图片链接 | `products.image_url` | Text URL; do not download automatically. |
| 库位 | `products.location_code` | Text; suspicious placeholder values require review. |
| 状态 | `products.status` | Map `启用`/`停用` to a controlled local value. |
| 品牌、分类、单位、产地、适用年龄、商品备注 | future/import metadata | No dedicated MVP field; retain in an import-row payload when import is implemented. |
| 盘点库存数量 | future inventory import | Never write inventory automatically in this phase. |
| 当前库存（导入时不需要录入） | no import | Explicitly ignore for product import. |

## Import safety gates for a future phase

- Treat blank JAN as legal and do not invent one.
- Detect duplicate non-empty JAN and duplicate non-empty QinSi product code separately.
- A numeric-looking identifier must still be read and written as text.
- Do not infer JAN from product code, name, URL, or any other field.
- Reject or quarantine shifted rows before any product creation.
- Never interpret `盘点库存数量` as purchased quantity or overwrite current stock with it.

Current inventory belongs to QinSi. Local QinSi export reservations record only exported quantities, statuses, and the contributing `receipt_items`; aggregation happens at export time and never mutates receipt purchase facts.
