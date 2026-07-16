from __future__ import annotations

from app.db import SessionLocal
from app.services import repair_confirmed_review_statuses


def main() -> None:
    with SessionLocal() as session:
        result = repair_confirmed_review_statuses(session)
    print("历史确认状态安全修复完成：" + "，".join(f"{key}={value}" for key, value in result.items()))


if __name__ == "__main__":
    main()
