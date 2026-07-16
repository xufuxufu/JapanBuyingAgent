# Code Map

| 业务模块 | 主要文件 | 核心函数或职责 | 相关测试文件 |
|---|---|---|---|
| 小票 | `app/main.py`、`app/services.py`、`app/models.py`、`app/schemas.py` | 上传、预处理、识别包、JSON 导入、审核、确认、原始资料留存 | `tests/test_receipts.py`、`tests/test_stage2.py`、`tests/test_async_upload.py`、`tests/test_workflow_dedup_zip.py` |
| 商品身份 | `app/product_identity.py`、`app/models.py`、`app/schemas.py` | `internal_sku` 分配、JAN/秦丝编码唯一性、名称规范、商品创建与更新 | `tests/test_products_and_health.py`、`tests/test_tracking_reservations.py` |
| 商品匹配 | `app/product_matching.py`、`app/qinsi_import.py` | `match_receipt`、`match_batch`、`bind_product`、别名匹配、秦丝商品导入预览 | `tests/test_product_import_matching_tracking.py` |
| 采购批次 | `app/purchase_service.py`、`app/main.py`、`app/models.py` | 创建/读取采购批次、确认采购来源、采购明细与目标仓库 | `tests/test_purchase_batches.py` |
| 店铺与追踪 | `app/store_service.py`、`app/models.py`、`app/main.py` | 品牌/门店主数据、小票高置信匹配、确认别名、商品与门店双向采购统计 | `tests/test_purchase_batches.py` |
| 位置 | `app/location_service.py`、`app/main.py`、`app/models.py` | 默认物理位置、秦丝仓库主数据、位置查询 | `tests/test_locations.py` |
| 秦丝导出 | `app/qinsi_export.py`、`app/qinsi_import.py`、`app/main.py` | 新商品/补货文件、来源追踪、结果确认、失败行重试 | `tests/test_qinsi_purchase_exports.py`、`tests/test_product_import_matching_tracking.py` |
| 在线查价 | `app/price_providers.py`、`app/price_service.py`、`app/main.py` | Provider 接口、JAN 查询、可信结果过滤、缓存、历史、店内价比较 | `tests/test_price_lookup.py` |
| 商品丰富化 | `app/product_enrichment.py`、`app/models.py`、`app/main.py` | 新 JAN 任务、Provider 候选、DeepSeek 名称、主图本地化、异常待办与安全建品 | `tests/test_product_enrichment.py` |
| 关注商品 | `app/watch_service.py`、`app/models.py`、`app/main.py` | 关注配置、推荐目标价、推荐生成、批量接受与启用 | `tests/test_product_watches.py` |
| Migration | `migrations/env.py`、`migrations/versions/`、`alembic.ini` | SQLite schema 演进、历史升级兼容、当前 head 管理 | `tests/test_migrations.py` |
| 页面入口 | `app/main.py`、`app/templates/`、`app/static/app.css` | FastAPI 路由、Jinja 页面、移动端样式；入口含小票、商品、采购、秦丝、查价 | 各业务测试中的页面 200 与表单流程测试 |
