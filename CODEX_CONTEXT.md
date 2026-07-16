# Codex Context

## 项目

- 项目：Japan Buying Agent
- 路径：`E:\xf\AIPJ\JapanBuyingAgent\japan-buying-agent-dev`
- 开发端口：`8020`
- 定位：日本实体采购辅助系统；秦丝负责销售与实时可售库存。
- 数据：项目独立 SQLite；Excel 仅用于导入、导出和字段参考。
- 当前 migration head：`20260716_0016`

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
- 秦丝：`app/qinsi_import.py`、`app/qinsi_export.py`
- 在线查价：`app/price_providers.py`、`app/price_service.py`
- 商品丰富化：`app/product_enrichment.py`
- 关注商品：`app/watch_service.py`
- 价格监控与通知：`app/monitor_service.py`、`app/monitor_scheduler.py`
- 页面与样式：`app/templates/`、`app/static/app.css`
- migration：`migrations/versions/`
- 测试：`tests/`

## 验证命令

- Quick：`scripts\verify_quick.bat [测试文件或节点表达式]`
- Core：`scripts\verify_core.bat`
- Full：`scripts\verify_full.bat`
- 低风险文档、CSS、普通排序或简单展示默认使用 Quick，不自动运行 Full。

## 禁止修改范围

- 不读取或修改视频项目及其数据库。
- 不操作 `8000`、`8013`。
- 不把本地系统改造成销售实时库存权威；秦丝仍是权威。
- 不擅自开发 `docs/NOT_NOW.md` 中项目。
- 单一任务只修改用户授权范围；产品决策不在执行任务中扩展讨论。
