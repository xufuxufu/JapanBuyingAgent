# Concentrated Fix Acceptance 2026-07-26

Deprecated historical acceptance note, last reviewed 2026-08-27. Use `../FEATURES_CURRENT.md` and `../KNOWN_ISSUES_AND_ROADMAP.md` for current status.

See also `docs/13_CONCENTRATED_FIX_ACCEPTANCE_20260726.md`.

## Verified By Automation

- Field purchase iPhone feedback contract: fixed high-z Toast `✓ 已识别 {JAN}`, scan guide green flash, AudioContext unlock/tone, success decode pause, full-frame fallback diagnostics, and static cache bump.
- Field purchase local lookup: single shared backend resolver with product barcode, main JAN, QinSi exact/derived JAN, and confirmed JAN alias coverage; request generation/AbortController; all terminal paths clear loading; 10s retryable failure.
- UI state: no `查询中 + 新品表单`; new draft only after explicit `NOT_FOUND`; compact next bar; persisted auto-continue switch; countdown/cancel only after save success; `deriveNextItemState()` remains authoritative.
- Rakuten provider: Item Search default endpoint `https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701`; Product Search opt-in; single auth request with `applicationId` and `accessKey` in query; sanitized platform self-check and one-shot button.
- Receipt traceability and previous provider stability remain unchanged.

## Test Results

- `py -3.14 -m py_compile app\main.py app\field_purchase.py app\local_product.py app\price_providers.py app\provider_config.py`: passed.
- Bundled Node `--check app/static/field_purchase.js`: passed. System default Node is too old for existing browser JS syntax, so bundled Node was used for this check.
- `.venv\Scripts\python.exe -m pytest -q tests\test_field_purchase_p0.py tests\test_price_lookup.py --basetemp=.pytest-concentrated-fix`: 29 passed.
- `verify_full` was not run.

## 8020 Verification

- `/health`: 200.
- `/field-purchase`: 200, serves `field-iphone-p0-20260726-2` and updated `开始扫码`/compact auto-continue UI.
- `/platform-config`: 200, shows `测试 Rakuten Item Search`, applicationId/accessKey presence and lengths, endpoint, and apiVersion.
- One Rakuten Item Search test was triggered through `/platform-config/rakuten/test`; result remained HTTP 403 auth failure. Program transmission format is now aligned to official Item Search/Test Form style, so a continuing 403 should be treated as credential/permission/config unless the same credentials pass the official Test Form.

## Awaiting Real iPhone Validation

- Camera capture return -> saving -> thumbnail -> local saved -> next enabled.
- Server draft upload failure -> server pending -> next remains enabled.
- Unknown JAN no longer shows loading and new-product form at the same time.
- Empty active batch start scan works from the same server-selected batch.
- Existing JAN `4548387688887` no longer stalls at local lookup and shows the registered product quickly.
