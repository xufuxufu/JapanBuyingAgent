# Next Chat

Deprecated historical handoff, last reviewed 2026-08-27. Use `../AI_HANDOFF.md` for current model-agnostic handoff.

## Start Here

- Current branch: `dev`.
- Alembic current/head verified: `20260722_0024`.
- Do not touch master, video/image project, 8000, or 8013.
- Current focused fix served on 8020 after restart: `field-iphone-p0-20260726-2`, service worker `jba-field-shell-v5`.

## What Is Done

- iPhone scan feedback: fixed Toast, green flash, AudioContext unlock/tone, and success decode pause.
- Field purchase local lookup: shared resolver with price-check, expanded local JAN coverage, loading cleanup on abort/error/timeout/success, retryable failure after 10s, and sanitized diagnostics.
- New-product form is now gated behind explicit local `NOT_FOUND`; no `查询中 + 新品表单`.
- Bottom bar is compact; auto-continue is a persisted small switch and starts only after save success.
- Rakuten Item Search request now uses endpoint/version `IchibaItem/Search/20260701`, `applicationId` + `accessKey` query params, no auth retry, Product Search default off, and platform-config self-check/button.
- Tests passed: py_compile, bundled Node `--check app/static/field_purchase.js`, `tests/test_field_purchase_p0.py`, and `tests/test_price_lookup.py`.
- 8020 checks: `/health`, `/field-purchase`, and `/platform-config` returned 200. One Rakuten Item Search test returned HTTP 403 with credentials present.

## What Remains

- iPhone real-device re-validation. Do not mark these fixes as iPhone-verified until the user confirms on the actual phone.
- On iPhone, re-scan existing JAN `4548387688887`, verify local hit in 0-2s and no stuck loading.
- If Rakuten official Test Form succeeds with the same credentials but the app still returns 403, compare request traces; if official Test Form also returns 403, treat as credential/permission/config, not code fixed.
