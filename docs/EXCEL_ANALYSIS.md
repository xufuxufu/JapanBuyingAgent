# Excel analysis

Analysis date: 2026-07-14. Both source files were copied unchanged to `reference/qinsi/`. Statistics use each sheet's full used range; visual checks use the header and first rows only.

## Workbook 1: `goodsImportTemplate秦丝导入模版.xlsx`

### Sheet `商品导入`

- Used range: 2,001 rows × 29 columns; row 1 is the header and 2,000 template rows follow.
- No formulas were found.
- Business-entry fields such as name, product code, barcode, model, brand, category, unit and minimum price are empty in all 2,000 rows.
- Default-filled fields: purchase price `0`, sale price `0`, sort `100`, status `启用`, points `启用` in all 2,000 rows.
- Inventory columns are present but empty: `盘点库存数量`, `当前库存（导入时不需要录入）`, `盘点仓库:`, `新日本仓库`.

Header sequence (29): 名称（必填）, 货号（必填且唯一）, 条码, 型号规格, 品牌, 分类, 单位, 采购价, 销售价, 最低销售价, 排序, 状态, 启用积分, 库存预警下限, 库存预警上限, 保质期（天）, 启用批次, 过期预警（天）, 商品图片链接, 商品备注, 产地, 适用年龄, 商品重量（KG）, 启用序列号, 库位, 盘点库存数量, 当前库存（导入时不需要录入）, 盘点仓库:, 新日本仓库.

### Sheet `配置`

- Used range: 100 rows × 84 columns; no formulas.
- This is a transposed lookup/config area, not a product table: row groups contain categories, units, warehouses, enable/disable values and brand-like headings.
- Examples include `默认分类`, units `个/件/双/条/盒`, warehouses `新日本仓库/无条码商品/2025招财猫/2025千羽/招财猫店`, and states `启用/停用`.
- The first column ends with a workbook marker `goodsImportTemplateA`.

## Workbook 2: `goodsImportTemplate已有商品模版-可到导入到本地数据库.xlsx`

### Sheet `商品导入`

- Used range: 2,001 rows × 30 columns; no formulas.
- Adds `商品规格` after `名称（必填）` compared with workbook 1, so column-position-only import logic is unsafe.
- Only two product rows contain names, product codes and barcodes; the remaining template rows mainly contain defaults.
- Example product 1: name `off relax夜间护发美容液10g*3`, product code `1234321`, barcode `4570110290418`, purchase `198`, sale `217`.
- Example product 2: name `怡丽丝尔睡眠面膜105g樱花`, product code `4901872097296`, barcode `4901872962495`, purchase `194`, sale `228`.
- Barcode and product code are demonstrably different fields, including the second row where both happen to look like 13-digit identifiers.
- Null profile: name/product-code/barcode have 2 non-empty and 1,998 empty data rows; purchase/sale/sort/status/points are default-filled in all 2,000 rows; minimum sale price is empty in all rows.

### Suspicious or shifted data

- Literal `184` appears across unrelated fields in the populated rows, including `商品规格`, `型号规格`, `单位`, `启用批次`, `商品备注`, `产地`, `适用年龄`, and `库位`.
- `184` is invalid or highly suspicious for unit, enable-batch, origin, age and location fields. These rows look column-shifted or contaminated and must be quarantined for manual review in any future import.
- One populated row has an empty brand while the other has `怡丽丝尔`.
- One `盘点库存数量` value is numeric zero; this must not trigger stock mutation.
- Image URLs include both HTTPS and HTTP; they are references only and should not be fetched automatically.

### Sheet `配置`

- Used range: 100 rows × 84 columns and structurally matches workbook 1.
- The first-column marker is `goodsImportTemplateB`; lookup values otherwise follow the same transposed layout.

## Conclusions

1. Map by normalized header name, never fixed column index.
2. `条码 → jan`; `货号（必填且唯一） → qinsi_product_code`.
3. Read both identifiers as strings even when Excel exposes numeric-looking values.
4. Empty JAN is legal; duplicate non-empty JAN is an error.
5. The two existing product rows are not safe for unattended import because of repeated `184` in semantically incompatible columns.
6. No Excel data is imported into the MVP database.

