# KNOWN_ISSUES_AND_ROADMAP.md

Last updated: 2026-08-27. Keep this file limited to unresolved items. Move completed work to `FEATURES_CURRENT.md` or `CHANGELOG.md`.

## Known Gaps

- Migration `20260828_0039`'s first version rebuilt `sales_orders` via SQLite `DROP TABLE`+`RENAME` in a way that could leave `sales_order_items.sales_order_id` pointing at a transient table name instead of `sales_orders`. The migration file has been corrected (drop-then-rename with `foreign_keys` disabled for the whole rebuild); the dev database was manually repaired and verified with `PRAGMA foreign_key_check` (empty result). `SO-20260827-0001`'s empty item list is a leftover symptom of this bug, not a new defect.
- Historical phase documents contain outdated statements such as "Receipt MVP", "future QinSi import/export", "no crawler/scheduler/notification", and iPhone re-validation notes. Use canonical docs first.
- `scripts/verify_quick.bat` and `scripts/verify_core.bat` compile a historical key-file list and do not include every newer module/migration. They are still useful, but pytest is the broader baseline.
- External provider health depends on credentials, permissions, allowed IP/referer, and rate limits. A 403/429 is not automatically a code regression.
- HEIC support depends on the installed Pillow build/decoder.
- Real QinSi import/export files and product images are business data and require manual migration/backups outside Git.

## Roadmap Candidates

- Keep QinSi master import conflict rules maintainable as more real shared-barcode cases are discovered.
- Add/update focused tests whenever modifying product identity, QinSi sync, receipt confirmation, or purchase export flows.
- Refresh old code-map and operations scripts after the next stable release so verification scripts include all current modules.
- Improve provider diagnostics without adding noise back to the primary field scanner.

## Not Roadmap

These are already implemented and should not remain as TODOs:

- Receipt schema `1.1` manual JSON import.
- Receipt confirmation to purchase batch.
- QinSi product-list header detection including Sheet1.
- QinSi conflict resolution history with auto-apply.
- Shared barcode candidates.
- Price check, enrichment, image localization, watches/notifications.
- QinSi inventory snapshots.
- Restock lists and purchase analytics.
