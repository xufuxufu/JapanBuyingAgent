# Current Implementation

Deprecated historical implementation snapshot, last reviewed 2026-08-27. Use `../FEATURES_CURRENT.md` and `../ARCHITECTURE.md` for the current implementation inventory.

Updated: 2026-07-26

## 已实现并自动验证

- Field purchase iPhone concentrated fixes: fixed top-level scan Toast `✓ 已识别 {JAN}` (>=1.2s), scan guide green flash, AudioContext unlock on `开始扫码`, success tone, success decode pause, and service-worker/static version bump to `field-iphone-p0-20260726-2` / `jba-field-shell-v5`.
- Field purchase local JAN lookup is still shared with `/price-check` through `resolve_local_product_by_jan()`. Coverage now includes `product_barcodes`, primary `Product.jan`, QinSi product code exact JAN, safe `/{JAN}` derived QinSi code, and confirmed JAN-like `ProductAlias`; JAN stays string-only.
- Local lookup and online providers are separated: `/api/field-purchase/lookup` returns only local DB state. New-product UI is rendered only after explicit `NOT_FOUND`; abort/timeout/error/success all clear loading; 10s frontend timeout enters retryable failure.
- Lookup diagnostics are sanitized: JAN, requestId suffix/client requestId, endpoint, elapsedMs, HTTP status, result state, match source, and error type/message only.
- Bottom action area is compact: `下一件` remains 52px-class height, auto-continue is a small persisted switch, and countdown appears only after successful save as `1秒后继续 · 取消`.
- Rakuten defaults to Item Search only (`IchibaItem/Search/20260701`); Product Search remains opt-in. Item Search sends `applicationId`, `accessKey`, `keyword`, `format=json`, `formatVersion=2`, `hits`, and `imageFlag` in one query request with no auth retry. `/platform-config` shows sanitized Item Search self-check and a single `测试 Rakuten Item Search` button.

## 8020 验证

- `GET /health`: 200.
- `GET /field-purchase`: 200, serves `field-iphone-p0-20260726-2`.
- `GET /platform-config`: 200, shows Rakuten Item Search endpoint/version and credential lengths.
- One manual `/platform-config/rakuten/test` POST was executed. Program request returned HTTP 403 authentication failure with credentials present; this now remains visible as configuration/permission/auth state, not hidden as a code success.

## 待真机复验

- iPhone Safari scan existing JAN `4548387688887`: Toast + green flash + tone appear immediately; local hit renders existing product in 0-2s and never shows `查询中 + 新品表单` together.
- iPhone Safari no-result decode fallback: after 3-5s diagnostics should show full-frame fallback attempts; camera remains live with frames increasing.
- Unknown JAN: only after local `NOT_FOUND` should new draft form appear.
- Auto-continue: default off, remembers last choice, starts only after successful existing scan/draft save, and can be canceled.

## 未实现

- Taobao auto-listing.
- Any changes to the video/image automation project.
