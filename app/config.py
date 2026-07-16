from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_DIR = DATA_DIR / "db"
ORIGINAL_DIR = DATA_DIR / "uploads" / "original"
PREVIEW_DIR = DATA_DIR / "uploads" / "preview"
PRODUCT_IMAGE_DIR = DATA_DIR / "products" / "main"
DEFAULT_DB_PATH = DB_DIR / "japan_buying_agent.sqlite3"


def database_url() -> str:
    return os.getenv("JBA_DATABASE_URL", f"sqlite:///{DEFAULT_DB_PATH.as_posix()}")


def ensure_data_directories() -> None:
    for path in (DB_DIR, ORIGINAL_DIR, PREVIEW_DIR, PRODUCT_IMAGE_DIR):
        path.mkdir(parents=True, exist_ok=True)
