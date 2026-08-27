# Japan Buying Agent

FastAPI/SQLite field purchasing assistant for Japanese buying work: receipts, local product handling, JAN/barcode lookup, price/enrichment, purchase batches, and QinSi import/export support.

## Documentation

New developer and AI handoff entry:

- [PROJECT_BIBLE.md](PROJECT_BIBLE.md) - positioning, boundaries, and rules that must not be broken.
- [AI_HANDOFF.md](AI_HANDOFF.md) - first file for Claude Code, Codex, ChatGPT, or another coding assistant.
- [BUSINESS_RULES.md](BUSINESS_RULES.md) - QinSi, JAN/barcode, receipt, purchase, image, and conflict rules.
- [ARCHITECTURE.md](ARCHITECTURE.md) - current modules, routes/services/templates/migrations/tests, and data flows.
- [FEATURES_CURRENT.md](FEATURES_CURRENT.md) - implemented feature inventory.
- [DATA_MODEL.md](DATA_MODEL.md) - core models, relationships, unique/index constraints.
- [OPERATIONS.md](OPERATIONS.md) - clone/setup/migration/start/test guide for a new machine.
- [KNOWN_ISSUES_AND_ROADMAP.md](KNOWN_ISSUES_AND_ROADMAP.md) - unresolved gaps only.

Older phase notes under `docs/` and `Japan_Buying_Agent/` are historical references. When they conflict with current code, migrations, tests, or the docs above, trust the current sources and update the stale note.

## Windows setup

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
run_dev_8020_py314.bat
```

Open `http://127.0.0.1:8020`. Health endpoint: `GET /health`.

The dev script does not use `--reload`; restart after Python code changes with `restart_dev_8020_py314.bat`.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
```

The default database is `data/db/japan_buying_agent.sqlite3`. Override only for this project with `JBA_DATABASE_URL`. Uploaded originals and previews stay under `data/uploads/`. Do not commit SQLite files, uploads, product images, `.env`, or private QinSi workbooks.

HEIC is accepted only when the installed Pillow build has a compatible HEIC decoder. Unsupported HEIC uploads return a clear message and do not create a batch.

## Receipt workflow

1. Upload one or more original images from camera or album.
2. Review original/processed comparisons and choose the recognition source.
3. Download an ordered recognition ZIP; this creates one independent task at `/gpt-jobs/{job_id}`. Use the fixed prompt in `docs/GPT_RECEIPT_PROMPT.md` manually with ChatGPT.
4. Paste schema 1.1 batch JSON on the GPT task page, preview exact `source_file` matches, then confirm one atomic multi-batch import. Historical schema 1.0 single-receipt imports remain compatible.
5. Edit the draft at `/receipts/{batch_id}/review`; amount mismatches are warnings and are never auto-corrected.
6. Final confirmation locks editing/deletion and records any confirmation warning. It does not update inventory.

Status definitions are in `docs/RECEIPT_STATUS_FLOW.md`.

## 2026-07-26 concentrated fix status

- Implemented and automatically verified: field purchase iPhone P0 state fixes, receipt/product traceability entry points, and Rakuten/Yahoo provider stabilization. See `docs/13_CONCENTRATED_FIX_ACCEPTANCE_20260726.md`.
- iPhone real-device status: the reported 2026-07-26 failures were reproduced from user evidence and fixed in code, but still require iPhone re-validation on the deployed 8020 page.
- Receipt traceability: product detail uses real `ReceiptItem.product_id` and `PurchaseBatchItem.receipt_item_id` links. `/tasks` now exposes a visible receipt entry; unmatched lines remain unmatched and are not fabricated by fuzzy matching.
- Rakuten provider: default JAN lookup uses Item Search `keyword=JAN`; Product Search is independent and only enabled by `JBA_RAKUTEN_PRODUCT_SEARCH_ENABLED=true`. 403, 429, empty results, and timeout are distinct states.
