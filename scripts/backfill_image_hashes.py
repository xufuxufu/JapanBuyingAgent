"""Incrementally fill image duplicate hashes without doing heavy work in migrations."""
from __future__ import annotations

import argparse

from sqlalchemy.orm import Session

from app.db import engine
from app.services import backfill_image_hashes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="maximum images to process in this run")
    args = parser.parse_args()
    with Session(engine) as session:
        count = backfill_image_hashes(session, args.limit)
    print(f"updated={count}")


if __name__ == "__main__":
    main()
