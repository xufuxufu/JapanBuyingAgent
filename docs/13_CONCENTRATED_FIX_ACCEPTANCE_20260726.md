# 2026-07-26 Concentrated Fix Acceptance

## 已实现并由自动测试验证

- 现场采购 iPhone P0：吊牌照片返回网页后先显示保存状态，读取 Blob、预览、IndexedDB 写入、服务器待同步状态均有安全诊断；本地保存成功即可下一件。
- 本地 JAN 查询：使用 request generation 和 AbortController，10 秒前端超时；旧响应不会覆盖新状态，失败显示重试，不进入新品表单。
- 下一件：`deriveNextItemState()` 统一按钮 enabled、reasonCode 和 message；手动下一件和自动下一件共用 `nextItem()`。
- 空批次：`/field-purchase` 无 query 时默认选最近 active 批次；真无批次时可自动创建临时批次并开始扫码。
- 扫码反馈：识别后停止本轮重复解码，显示“已识别 JAN”的浮层，播放短提示音；振动仅作为可用时增强。
- 小票追溯：`/tasks`、侧栏和更多页有小票记录入口；小票详情展示真实商品行，已匹配行可跳商品详情；商品详情展示来源小票和对应采购批次。
- Provider：Rakuten 默认只调用 Item Search `keyword=JAN`；Product Search 需显式启用。Provider 调用有 in-flight 合并，429 映射限流 cooldown，403 映射认证/权限，0 条保留正常空结果。

## 自动测试

- `py -3.14 -m py_compile app\main.py app\price_providers.py app\price_service.py app\provider_config.py tests\test_field_purchase_p0.py tests\test_price_lookup.py`
- bundled Node `--check app\static\field_purchase.js`
- `tests\test_field_purchase_p0.py`: 19 passed
- `tests\test_price_lookup.py`: 9 passed
- `tests\test_receipts.py tests\test_product_import_matching_tracking.py`: 25 passed
- `scripts\verify_core.bat`: passed when bundled Node is placed first in PATH
- `http://127.0.0.1:8020/health`: 200, database ok

## 已实现但待 iPhone 真机复验

- iPhone 系统相机确认照片后，网页是否立即显示“正在保存”、缩略图和“已保存到本机”。
- 模拟服务器同步失败时，是否显示“服务器待同步”且允许下一件。
- JAN `4548387689099` 等未知码是否不再出现“查询中 + 新品表单”非法组合。
- 旧静态资源缓存是否已刷新到 `field-iphone-p0-20260726-2` / `jba-field-shell-v5`。

## 尚未实现 / 已知限制

- 未执行真实 Rakuten 调用；当前禁止频繁真实调用，最后人工测试应只从 `/platform-config` 单次触发。
- 未做淘宝自动上架，未修改任何视频图片项目或 8000/8013 服务。
- 未对无显式关联的历史小票行做名称模糊回填；保持未匹配，等待人工处理。
