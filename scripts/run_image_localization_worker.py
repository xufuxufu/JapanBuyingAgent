from __future__ import annotations

import argparse

from app.db import engine
from app.image_localization_worker import run_image_localization_worker


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the QinSi product image localization worker without a browser.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process all currently available jobs, then exit.",
    )
    args = parser.parse_args()
    result = run_image_localization_worker(engine, once=args.once)
    print(f"processed={result.processed} batches={result.batches}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
