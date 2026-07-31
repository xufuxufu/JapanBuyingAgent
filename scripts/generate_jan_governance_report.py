from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import SessionLocal
from app.jan_governance import export_jan_governance_report


def main() -> None:
    with SessionLocal() as session:
        report = export_jan_governance_report(session, apply_safe_fixes=True)
    print(json.dumps({
        "conflict_count": report.conflict_count,
        "auto_fixed_count": report.auto_fixed_count,
        "csv_path": str(report.csv_path.resolve()),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
