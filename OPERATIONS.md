# OPERATIONS.md

Last updated: 2026-08-27.

## Clone and Branch

```powershell
git clone https://github.com/xufuxufu/JapanBuyingAgent.git
cd JapanBuyingAgent
git switch dev
```

`dev` is the normal development branch. `master` is the stable baseline branch.

## Branch Strategy

- `master` is the current stable baseline for the new ThinkBook migration.
- `claude-code` will be created later by Claude Code from `master` and used as its long-running development branch.
- `dev` remains as the Codex/backup development branch.

Do not use `master` as a long-running development branch. Only merge verified stable work into `master`. `dev` and `claude-code` do not need continuous synchronization; merge between them only through explicit tasks.

## Python and Dependencies

Current setup scripts expect Python 3.14 and a local `.venv`:

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Dependencies are defined in `requirements.txt`. There is currently no `pyproject.toml`.

## Database and Migrations

Default database path:

```text
data\db\japan_buying_agent.sqlite3
```

Config resolution is in `app/config.py`: `JBA_DATABASE_URL` can override the default for this project. Alembic default URL is also `sqlite:///data/db/japan_buying_agent.sqlite3`.

Run migrations:

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
```

Do not commit SQLite files or database backups. Current real database files should be copied between machines as runtime data, not checked into Git.

## Start, Restart, Health

Start dev server:

```powershell
.\run_dev_8020_py314.bat
```

Restart after Python code changes:

```powershell
.\restart_dev_8020_py314.bat
```

The server runs on `http://127.0.0.1:8020`.

Health check:

```powershell
Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8020/health
```

Important: current dev server does not use `--reload`. Python changes require restart.

## Tests

Full pytest:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.pytest-tmp
```

Quick targeted script:

```powershell
.\scripts\verify_quick.bat
.\scripts\verify_quick.bat tests\test_qinsi_product_master_import.py
```

Core verification script:

```powershell
.\scripts\verify_core.bat
```

`verify_core.bat` compiles key files, checks Alembic heads/current, runs core regression tests, restarts 8020, and checks `/health`.

## Runtime Data Migration to a New ThinkBook

Safe migration checklist:

1. Clone repo and install dependencies on the new machine.
2. Copy runtime data outside Git:
   - `data\db\japan_buying_agent.sqlite3`
   - intentional database backups;
   - `data\uploads\` receipt originals/previews;
   - `data\products\main\` manual/main product images;
   - `data\products\qinsi-localized\` localized QinSi product images;
   - `data\field-purchases\tag-evidence\` field-purchase tag/photo evidence;
   - `data\reports\` if historical generated CSV/XLSX reports are needed.
3. Keep paths relative to the project where possible. Do not bake old-machine absolute paths into docs or code.
4. Start with `.\restart_dev_8020_py314.bat`.
5. Open `/health`, `/products`, `/receipts`, `/field-purchase`, and `/products/qinsi-master-import`.
6. Run targeted tests before any new development.

Do not copy `.venv` between machines unless deliberately debugging environment parity. Recreate it from `requirements.txt`.

## Secrets and Environment

Use `.env` or machine-level environment variables for provider credentials. `.env.example` documents `JBA_DATABASE_URL`; current code also reads provider-specific settings from `app/config.py`.

Never commit secrets, `.env`, database files, uploaded images, or generated QinSi workbooks containing private business data.

Do not migrate generated caches or rebuildable local state such as `.venv`, `__pycache__`, `.pytest_cache`, `.pytest-tmp*`, `logs`, `.tmp`, node/pip caches, or other temporary build output.
