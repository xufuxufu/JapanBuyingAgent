"""One-time setup: download the CLIP ONNX vision encoder used by the image
search feature (app/image_search.py) into data/image-search/models/.

The model file (~85 MB, int8-quantized) is intentionally NOT committed to
git (see .gitignore) — run this script once per machine/checkout instead:

    .venv\\Scripts\\python.exe scripts\\prepare_image_search_model.py

Source: Xenova/clip-vit-base-patch32 (ONNX export of OpenAI's CLIP ViT-B/32),
the same weights validated in the Phase-9 image-search benchmark
(Top1 90.0% / Top3+ 93.3% on a 400-product confusable-category sample).
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import IMAGE_SEARCH_MODEL_DIR

BASE_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main"
FILES = {
    "onnx/vision_model_quantized.onnx": "clip-vit-b32-vision-int8.onnx",
    "preprocessor_config.json": "preprocessor_config.json",
}


def download(url: str, dest: Path) -> None:
    opener = urllib.request.build_opener()
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (jba-model-prep)")]
    print(f"downloading {url} -> {dest}")
    with opener.open(url, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    print(f"  saved {len(data):,} bytes")


def main() -> None:
    IMAGE_SEARCH_MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for remote_path, local_name in FILES.items():
        dest = IMAGE_SEARCH_MODEL_DIR / local_name
        if dest.exists() and dest.stat().st_size > 0:
            print(f"already present, skipping: {dest}")
            continue
        download(f"{BASE_URL}/{remote_path}", dest)
    print("done. Model ready at:", IMAGE_SEARCH_MODEL_DIR)


if __name__ == "__main__":
    main()
