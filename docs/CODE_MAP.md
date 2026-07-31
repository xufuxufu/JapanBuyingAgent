# Code Map

| 业务模块 | 主要文件 | 核心函数或职责 | 相关测试文件 |
|---|---|---|---|
| 小票 | `app/main.py`、`app/services.py`、`app/models.py`、`app/schemas.py`、`app/templates/detail.html`、`app/templates/product_enrichment.html` | 上传、预处理、识别包、JSON 导入、审核、确认、原始资料留存；任务页小票入口；小票详情真实商品行 | `tests/test_receipts.py`、`tests/test_stage2.py`、`tests/test_async_upload.py`、`tests/test_workflow_dedup_zip.py`、`tests/test_product_import_matching_tracking.py` |
| 商品身份 | `app/product_identity.py`、`app/models.py`、`app/schemas.py` | `internal_sku` 分配、JAN/秦丝编码唯一性、名称规范、商品创建与更新 | `tests/test_products_and_health.py`、`tests/test_tracking_reservations.py` |
| 商品匹配 | `app/product_matching.py`、`app/local_product.py`、`app/qinsi_goods_import.py` | `match_receipt`、`match_batch`、`bind_product`、统一 JAN 多源解析、秦丝商品导入预览 | `tests/test_product_import_matching_tracking.py`、`tests/test_qinsi_goods_import.py` |
| 采购批次 | `app/purchase_service.py`、`app/main.py`、`app/models.py` | 创建/读取采购批次、确认采购来源、采购明细与目标仓库 | `tests/test_purchase_batches.py` |
| 店铺与追踪 | `app/store_service.py`、`app/models.py`、`app/main.py` | 品牌/门店主数据、小票高置信匹配、确认别名、商品与门店双向采购统计 | `tests/test_purchase_batches.py` |
| 位置 | `app/location_service.py`、`app/main.py`、`app/models.py` | 默认物理位置、秦丝仓库主数据、位置查询 | `tests/test_locations.py` |
| 秦丝导出 | `app/qinsi_export.py`、`app/qinsi_import.py`、`app/main.py` | 新商品/补货文件、来源追踪、结果确认、失败行重试 | `tests/test_qinsi_purchase_exports.py`、`tests/test_product_import_matching_tracking.py` |
| 在线查价 | `app/price_providers.py`、`app/price_service.py`、`app/main.py` | Provider 接口、JAN 查询、可信结果过滤、缓存、历史、店内价比较 | `tests/test_price_lookup.py` |
| 商品丰富化 | `app/product_enrichment.py`、`app/models.py`、`app/main.py` | 新 JAN 任务、Provider 候选、DeepSeek 名称、主图本地化、异常待办与安全建品 | `tests/test_product_enrichment.py` |
| 关注商品 | `app/watch_service.py`、`app/models.py`、`app/main.py` | 关注配置、推荐目标价、推荐生成、批量接受与启用 | `tests/test_product_watches.py` |
| 价格监控与通知 | `app/monitor_service.py`、`app/monitor_scheduler.py`、`app/main.py`、`app/models.py` | 到期扫描、可信价格快照、达价事件、网页通知与失败退避 | `tests/test_price_monitoring.py` |
| 秦丝库存快照 | `app/qinsi_inventory.py`、`app/qinsi_import.py`、`app/main.py`、`app/models.py` | 不可覆盖库存快照、精确商品/仓库匹配、最近快照聚合与采购辅助判断 | `tests/test_qinsi_inventory_snapshots.py` |
| 补货清单 | `app/restock_service.py`、`app/main.py`、`app/models.py` | 按具体门店生成/维护现场补货清单、候选排序、临时购买结果、正式小票关联回溯 | `tests/test_restock_lists.py` |
| 现场采购 P0 | `app/field_purchase.py`、`app/local_product.py`、`app/main.py`、`app/models.py`、`app/templates/field_purchase.html`、`app/static/field_purchase.js`、`app/static/camera_adapter.js` | 统一本地 JAN 查询、秦丝货号派生条码、可选门店、现场采购事实、共享相机适配、IndexedDB 幂等同步、iPhone 照片安全诊断、下一件统一状态、可恢复后台任务、人工确认 | `tests/test_field_purchase_p0.py` |
| JAN 治理 | `app/jan_governance.py`、`app/local_product.py`、`scripts/generate_jan_governance_report.py` | 冲突检测、确定性一对一派生别名修复、UTF-8 CSV 与在线入口 | `tests/test_jan_governance.py` |
| 商品图片本地化 | `app/product_image_localization.py`、`app/models.py`、`app/main.py`、`app/static/service-worker.js` | durable job 下载、SSRF/格式/大小校验、SHA-256 去重、原子写入、图片回退与现场离线缓存 | `tests/test_product_image_localization.py` |
| 平台配置 | `app/provider_config.py`、`app/price_providers.py`、`app/price_service.py` | Local/Qinsi、Rakuten、Yahoo、Amazon Creators 占位、Manual Provider；配置状态与 SEARCH_ONLY 降级；Rakuten Item/Product endpoint 独立状态、in-flight 去重、429 cooldown/403/0 条语义 | `tests/test_price_lookup.py`、`tests/test_field_purchase_p0.py` |
| Migration | `migrations/env.py`、`migrations/versions/`、`alembic.ini` | SQLite schema 演进、历史升级兼容、当前 head 管理 | `tests/test_migrations.py` |
| 页面入口 | `app/main.py`、`app/templates/`、`app/static/app.css` | FastAPI 路由、Jinja 页面、移动端样式；入口含小票、商品、采购、秦丝、查价 | 各业务测试中的页面 200 与表单流程测试 |
