# Field Purchase Handoff 2026-07-26

Deprecated historical handoff, last reviewed 2026-08-27. Use `../AI_HANDOFF.md`, `../FEATURES_CURRENT.md`, and `../BUSINESS_RULES.md` for current behavior.

## Root Fixes

- iPhone scan feedback is visible outside the video frame: fixed high-z Toast `✓ 已识别 {JAN}`, green scan-frame flash, AudioContext unlock on `开始扫码`, short success tone, and about 950ms decode pause after success. It does not depend on vibration; Android vibration remains an optional enhancement.
- Local lookup has request generation, AbortController, 10s frontend timeout, and explicit loading cleanup on success/error/abort/timeout. `abortLocalLookup()` now clears `local_query_loading`, fixing the stuck `正在查询本地商品` state.
- `renderNew()` is reachable only after explicit local `NOT_FOUND`; query loading and new-product form are mutually exclusive.
- `/field-purchase` and `/price-check` use the same backend resolver: `resolve_local_product_by_jan()`. Matching covers `product_barcodes`, main JAN, QinSi exact/derived JAN, and confirmed JAN aliases.
- Sanitized lookup diagnostics are available in frontend debug and backend logs: JAN, requestId, endpoint, elapsedMs, HTTP status, result state, match source, and error only.
- `deriveNextItemState()` remains the single source for next-button enabled/reason/message. Server image/provider/AI sync does not block next item once local draft/tag evidence is saved.
- `/field-purchase` defaults to the newest active batch if no `batch_id` is supplied, so header and body do not diverge.
- Bottom action area is compact; auto-continue is a persisted small switch and only starts after successful save.
- Static cache updated to `field-iphone-p0-20260726-2` and `jba-field-shell-v5`.

## Verification

- `py -3.14 -m py_compile app\main.py app\field_purchase.py app\local_product.py app\price_providers.py app\provider_config.py`: passed.
- Bundled Node: `node --check app/static/field_purchase.js`: passed.
- `.venv\Scripts\python.exe -m pytest -q tests\test_field_purchase_p0.py tests\test_price_lookup.py --basetemp=.pytest-concentrated-fix`: 29 passed.
- `verify_full` was not run.

## iPhone Checklist

- Scan existing JAN `4548387688887` -> immediate Toast/green flash/tone -> local hit -> purchase fact saved -> next enabled.
- Confirm no `正在查询本地商品` and new-product form are visible together.
- Scan unknown JAN -> lookup exits loading -> new draft only after not-found.
- Capture tag photo -> saving state -> thumbnail -> local saved -> next enabled.
- Disable server/network after local save -> server pending shown -> next still enabled.
- Let iPhone camera run 3-5s without result -> diagnostic should show full-frame fallback; `拍吊牌识别 JAN` remains available and is clearly a JAN fallback.
