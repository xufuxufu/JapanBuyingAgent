# AI_HANDOFF.md

Last updated: 2026-08-27. This file is model-agnostic for Claude Code, Codex, ChatGPT, or another coding assistant.

## Start Here

Before coding, read these first:

1. [PROJECT_BIBLE.md](PROJECT_BIBLE.md)
2. [AI_HANDOFF.md](AI_HANDOFF.md)
3. [BUSINESS_RULES.md](BUSINESS_RULES.md)
4. The task-relevant doc: usually [ARCHITECTURE.md](ARCHITECTURE.md), [DATA_MODEL.md](DATA_MODEL.md), [FEATURES_CURRENT.md](FEATURES_CURRENT.md), or [OPERATIONS.md](OPERATIONS.md)

If old docs disagree with code/tests/migrations, trust code/tests/migrations and update docs only after verifying.

## Current State

JBA is a FastAPI/SQLite field purchasing system on branch `dev`, running locally on port `8020`. The project is beyond the original Receipt MVP: it includes receipts, products, field purchase, QinSi product master import/export, purchase batches, QinSi purchase exports, price lookup, enrichment, image localization, inventory snapshots, watches/notifications, restock lists, analytics, stores, and locations.

## Branch Strategy

- `master`: current stable baseline. Claude Code should first clone from here on the new ThinkBook.
- `claude-code`: future long-running Claude Code development branch, created by Claude Code on the new machine from `master`.
- `dev`: Codex/backup development branch, retained for now.

Claude Code should not use `master` as its long-running development branch. `master` only receives verified stable code. `dev` and `claude-code` do not need to stay continuously synchronized; later merges must be explicit tasks and must not automatically overwrite either branch.

Current server scripts:

- `run_dev_8020_py314.bat`
- `restart_dev_8020_py314.bat`

Python changes require restart because the dev script does not use `--reload`.

## Common Entrypoints

- `app/main.py`: routes and page wiring.
- `app/models.py`: SQLAlchemy model source of truth.
- `migrations/versions/`: schema history.
- `tests/`: behavior baseline; tests are often newer than old docs.
- `app/local_product.py`: JAN/barcode lookup rules.
- `app/qinsi_product_master_import.py` and `app/qinsi_goods_import.py`: QinSi product import rules.
- `app/purchase_service.py` and `app/qinsi_export.py`: purchase batch and QinSi export rules.
- `app/price_service.py`, `app/price_providers.py`, `app/product_enrichment.py`: price/enrichment flow.
- `app/field_purchase.py`: mobile field-purchase capture and review.

## Do Not Rebuild These

- Receipt upload, image provenance, schema `1.1` import, review, and confirmation.
- Product identity, nullable unique JAN, QinSi code separation, duplicate JAN governance, and placeholder cleanup.
- Shared barcode handling through `product_barcodes`.
- QinSi master import preview/confirm/conflict history/audit workbook.
- Receipt to purchase-batch creation.
- QinSi purchase and product export job loops.
- Price lookup, provider attempts, web fallback, enrichment, image localization.
- Watches/notifications, inventory snapshots, restock lists, stores/locations, analytics.

## Development Rules

- Prefer small, task-scoped changes.
- Do not edit business data or run destructive database operations.
- Do not commit runtime data: SQLite, uploads, images, generated private Excel files, `.env`, or secrets.
- When changing models, add an Alembic migration and migration tests.
- When changing identity, receipt, QinSi, or purchase facts, add focused regression tests.
- Preserve purchase facts. Product sync may update product-master fields but must not rewrite historical receipt or purchase evidence.
- Keep documentation model-agnostic. Do not encode a Codex-only or Claude-only workflow unless clearly labeled as optional tooling.

## Verification

Typical checks:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
.\scripts\verify_quick.bat tests\test_qinsi_product_master_import.py
.\scripts\verify_core.bat
```

For docs-only changes, at minimum inspect `git diff` and confirm no business code or runtime data changed.

Current test baseline should be reported from the latest run in the task summary. If tests were not run, say exactly that.

## Done Report Format

- Changed: files and purpose.
- Verified: commands run and results.
- Data safety: whether business code/data was untouched.
- Notes: stale docs, assumptions, or follow-up risks.
