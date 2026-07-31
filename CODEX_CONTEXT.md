# Codex Context

## 项目

- 项目：Japan Buying Agent
- 路径：`E:\xf\AIPJ\JapanBuyingAgent\japan-buying-agent-dev`
- 开发端口：`8020`
- 定位：日本实体采购辅助系统；秦丝负责销售与实时可售库存。
- 数据：项目独立 SQLite；Excel 仅用于导入、导出和字段参考。
- 当前 migration head：`20260722_0024`

## 核心业务流程

1. 上传并保留小票原图，生成预处理图。
2. 手动把识别包交给 ChatGPT，导入并保存原始 JSON。
3. 人工审核异常，确认小票与商品行。
4. 使用 JAN、秦丝商品编码、别名等匹配商品。
5. 确认采购后生成采购批次，并指定位置与秦丝目标仓库。
6. 按新商品或补货生成秦丝文件，确认导入结果，失败行可重试。
7. 手机扫码或输入 JAN，优先匹配本地商品，再查询当前线上价格。

## 已完成主要功能

- 小票上传、图片预处理、重复检测、识别 ZIP 与手动 JSON 导入。
- 小票审核、确认、历史追踪与原始资料保留。
- 商品导入、稳定身份、JAN/秦丝编码去重、商品匹配。
- 采购批次、位置主数据、秦丝目标仓库。
- 店铺品牌、具体门店、小票门店匹配与商品采购双向追踪。
- 秦丝新商品/补货导出、结果确认、失败行重试闭环。
- JAN 扫码查价、Rakuten/Yahoo/Manual Provider、缓存与查询历史。

## 核心身份规则

- 商品主身份是稳定 `internal_sku`，格式由系统生成且不得回写替换。
- JAN 是可空文本，保留前导零；不得伪造。
- 秦丝商品编码与 JAN 独立，不能互相复制或代替。
- JPY 金额使用整数日元。
- 商品展示名固定为 `中文名｜日文名`；中文名、日文名独立保存，各最长 128 字符。
- 在线结果不得覆盖人工确认的商品资料。

## 主要代码入口

- 应用与路由：`app/main.py`
- 数据模型：`app/models.py`
- 输入输出结构：`app/schemas.py`
- 小票服务：`app/services.py`
- 商品身份/匹配：`app/product_identity.py`、`app/product_matching.py`
- 采购/位置：`app/purchase_service.py`、`app/location_service.py`
- 秦丝：`app/qinsi_goods_import.py`、`app/qinsi_import.py`、`app/qinsi_export.py`
- 在线查价：`app/price_providers.py`、`app/price_service.py`
- 商品丰富化：`app/product_enrichment.py`
- 关注商品：`app/watch_service.py`
- 价格监控与通知：`app/monitor_service.py`、`app/monitor_scheduler.py`
- 秦丝库存快照与采购辅助：`app/qinsi_inventory.py`
- 补货清单：`app/restock_service.py`
- 现场采购/离线幂等/可恢复任务：`app/field_purchase.py`
- 平台配置状态：`app/provider_config.py`
- 本地 JAN 统一解析与秦丝货号派生别名：`app/local_product.py`
- JAN 治理报告：`app/jan_governance.py`、`scripts/generate_jan_governance_report.py`
- 秦丝商品图片本地化：`app/product_image_localization.py`
- 页面与样式：`app/templates/`、`app/static/app.css`
- migration：`migrations/versions/`
- 测试：`tests/`

## 验证命令

- Quick：`scripts\verify_quick.bat [测试文件或节点表达式]`
- Core：`scripts\verify_core.bat`
- Full：`scripts\verify_full.bat`
- 低风险文档、CSS、普通排序或简单展示默认使用 Quick，不自动运行 Full。

## 秦丝库存快照配置

- `JBA_QINSI_SNAPSHOT_STALE_HOURS`：快照过期小时数，默认 `72`。
- `JBA_QINSI_DEFAULT_LOW_STOCK_THRESHOLD`：默认低库存阈值，默认 `3`。
- `JBA_QINSI_SNAPSHOT_MAX_UPLOAD_MB`：上传上限，默认/最大 `20` MB。
- `JBA_QINSI_SNAPSHOT_EXTENSIONS`：允许扩展名，默认 `.xlsx`。
- `JBA_QINSI_REUSE_DUPLICATE_FILE`：重复哈希复用原快照，默认启用。
- `JBA_PURCHASE_ASSISTANCE_ENABLED`：采购辅助提示，默认启用。

## 现场采购与平台配置

- 现场入口：`/field-purchase`；先查 `product_barcodes/products`，不等待 OCR、AI 或平台。
- JAN 解析状态统一为 `UNIQUE`、`NOT_FOUND`、`AMBIGUOUS`；多匹配必须人工选择或暂存审核。
- 图片本地化任务只保存文件到 `data/products/qinsi-localized/`，页面优先本地图；现场仅缓存已扫商品或当前采购批次。
- 手机草稿和图片任务先写 IndexedDB；服务端用 `client_request_id` 幂等。
- 吊牌证据保存在 `data/field-purchases/tag-evidence/`，该目录被 Git 忽略。
- 数据库任务支持 pending/running 回收与重启恢复；审核入口：`/tasks`。
- Provider 凭证统一列在 `.env.example`；平台状态入口：`/platform-config`，不回显完整 Key。
- 现场批次门店可稍后补充；未填写时保持 `NULL`，不创建虚假门店。
- 2026-07-26 集中修复后：`/field-purchase` 无 batch_id 时默认使用最近 active 批次，避免页头和主体批次源不一致；前端使用 `deriveNextItemState()` 统一下一件条件。
- 吊牌照片在 iPhone 返回网页后先写 IndexedDB，本地成功独立于服务器同步；安全诊断仅记录文件名、MIME、大小、读取/预览/IndexedDB/sync 状态和错误，不输出图片内容。
- 本地 JAN 查询有 10 秒前端硬超时、request generation/AbortController 防旧响应覆盖；失败显示可重试，不显示新品表单。
- Rakuten 默认只启用 Item Search `keyword=JAN`；Product Search 是显式配置能力。429 进入 cooldown 语义并不立即重试，403 是认证/权限错误，0 条是正常空结果。
- `/tasks` 和导航已补小票记录入口；小票详情展示真实商品行，商品详情展示真实来源小票和对应采购批次。未匹配行保持未匹配，不按名称猜测回填。

## 禁止修改范围

- 不读取或修改视频项目及其数据库。
- 不操作 `8000`、`8013`。
- 不把本地系统改造成销售实时库存权威；秦丝仍是权威。
- 不擅自开发 `docs/NOT_NOW.md` 中项目。
- 单一任务只修改用户授权范围；产品决策不在执行任务中扩展讨论。
