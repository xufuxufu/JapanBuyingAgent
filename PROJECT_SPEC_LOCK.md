# Project Specification Lock

Deprecated historical lock, last reviewed 2026-08-27. This file contains early phase restrictions that are no longer current, including statements that price search, monitoring, and formal QinSi import are not implemented. Use `PROJECT_BIBLE.md`, `BUSINESS_RULES.md`, and `AI_HANDOFF.md` as the current source.

## Identity and isolation

- Project: `Japan Buying Agent`
- Development port: `8020`
- Target: `E:\xf\AIPJ\JapanBuyingAgent\japan-buying-agent-dev`
- This is not a module of the video automation project.
- It has its own source tree, virtual environment, SQLite database, uploads, migrations, and tests.

## Locked data rules

1. The database is the sole application source of truth.
2. Excel is limited to import/export and field reference.
3. `products.jan` and `products.qinsi_product_code` are distinct identifiers.
4. JAN is nullable text; non-null values are unique and leading zeroes are preserved.
5. QinSi product code is nullable text; non-null values are unique.
6. All JPY amounts are integer yen.
7. Original receipt images and accepted raw GPT responses are retained.
8. No inventory mutation occurs before human confirmation.
9. MVP 1 does not update QinSi inventory, call a paid GPT API, search prices, or monitor prices.
10. Manual GPT JSON import must validate fully and commit atomically.
11. Phase 2 may preprocess images and support human receipt confirmation, but must not update inventory or create products.
12. Phase 2 must not formally import QinSi data, call paid GPT APIs, search prices, or monitor prices.
