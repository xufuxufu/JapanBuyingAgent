# Agent Rules

This repository is the independent **Japan Buying Agent** project.

- Never read, import, modify, overwrite, or connect to the video-processing project or its database.
- Persist application state in this project's SQLite database only (DB-only).
- Excel is reference/import/export material, never the live datastore.
- Keep JAN and QinSi product code separate. Never infer or copy one into the other.
- Store JAN as text and preserve leading zeroes. Never fabricate a JAN.
- Store all JPY money as integer yen, never floating point.
- Preserve every original receipt image byte-for-byte.
- Preserve every accepted raw GPT JSON response.
- Never change inventory before explicit human confirmation.
- In this phase, do not update QinSi inventory, call paid GPT APIs, search prices, or monitor prices.
- In this phase, do not automatically create products or formally import data into QinSi.
- Run migrations, compilation, and the complete test suite before declaring the change ready.

## Codex development workflow

- Read `CODEX_CONTEXT.md` first for every task.
- Then use `docs/CODE_MAP.md` to read only the files related to the task; do not scan the whole repository by default.
- Use `scripts/verify_quick.bat`, `scripts/verify_core.bat`, and `scripts/verify_full.bat` for graded validation.
- Low-risk tasks must not automatically run the full test suite.
- Prefer extending existing tests instead of creating duplicate test coverage.
- Do not add tests solely for CSS, ordinary sorting, or simple display changes.
- Final replies must not paste source code or complete logs.
