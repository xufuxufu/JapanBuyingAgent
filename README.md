# Japan Buying Agent — Receipt MVP

Independent FastAPI/SQLite MVP for uploading Japanese receipt images, retaining originals, viewing receipt history, and transactionally importing manually obtained GPT JSON.

## Windows setup

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m alembic upgrade head
run_dev_8020_py314.bat
```

Open `http://127.0.0.1:8020`. Health endpoint: `GET /health`.

## Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
```

The default database is `data/db/japan_buying_agent.sqlite3`. Override only for this project with `JBA_DATABASE_URL`. Uploaded originals and previews stay under `data/uploads/`.

HEIC is accepted only when the installed Pillow build has a compatible HEIC decoder. Unsupported HEIC uploads return a clear message and do not create a batch.

## Receipt workflow

1. Upload one or more original images from camera or album.
2. Review original/processed comparisons and choose the recognition source.
3. Download an ordered recognition ZIP; this creates one independent task at `/gpt-jobs/{job_id}`. Use the fixed prompt in `docs/GPT_RECEIPT_PROMPT.md` manually with ChatGPT.
4. Paste schema 1.1 batch JSON on the GPT task page, preview exact `source_file` matches, then confirm one atomic multi-batch import. Historical schema 1.0 single-receipt imports remain compatible.
5. Edit the draft at `/receipts/{batch_id}/review`; amount mismatches are warnings and are never auto-corrected.
6. Final confirmation locks editing/deletion and records any confirmation warning. It does not update inventory.

Status definitions are in `docs/RECEIPT_STATUS_FLOW.md`.
