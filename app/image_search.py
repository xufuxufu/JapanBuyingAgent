"""Image-to-image product search: ONNX CLIP (ViT-B/32, int8 vision tower)
+ FAISS IndexFlatIP.

Validated in the Phase-9 benchmark on this machine (CPU only, no dGPU):
Top1 90.0%, Top3/Top5/Top10 93.3% on a 400-product confusable-category
sample; ~26ms per search; ~231MB RSS. See the benchmark report for detail.

Design:
- The ONNX model and the FAISS index are process-wide lazy-loaded
  singletons (double-checked locking) -- never reloaded per request, never
  rebuilt per request.
- Index files live under data/image-search/ (gitignored). Building writes
  to temp files first and only atomically replaces the real files once the
  whole build succeeds, so a failed/interrupted build never damages the
  live index.
- This module never decides "the right product" -- it only ranks
  candidates. Callers must always present a top-K list for a human to pick
  from; never auto-accept the top-1 result.
"""
from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.config import (
    IMAGE_SEARCH_DIR, IMAGE_SEARCH_MODEL_DIR, PRODUCT_IMAGE_DIR, PROJECT_ROOT, QINSI_PRODUCT_IMAGE_DIR, env_int,
)
from app.models import Product
from app.product_image_localization import _stored_product_image_exists, download_remote_image, product_display_image

logger = logging.getLogger(__name__)

MODEL_PATH = IMAGE_SEARCH_MODEL_DIR / "clip-vit-b32-vision-int8.onnx"
INDEX_PATH = IMAGE_SEARCH_DIR / "products.faiss"
MAP_PATH = IMAGE_SEARCH_DIR / "product_image_map.json"
META_PATH = IMAGE_SEARCH_DIR / "index_meta.json"

MODEL_NAME = "clip-vit-b32-int8-vision-onnx"
IMAGE_SIZE = 224
IMAGE_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
IMAGE_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

MAX_QUERY_IMAGE_BYTES = 10 * 1024 * 1024
ALLOWED_QUERY_FORMATS = {"JPEG", "PNG", "WEBP"}
TOP_K_DEFAULT = 10
TOP_K_MAX = 20
BUILD_DOWNLOAD_TIMEOUT_SECONDS = 6.0
# Fetch (download/local-read) concurrency for index building. Bounded on
# purpose -- this is a courtesy limit for remote hosts we don't control, not
# a performance dial to max out. 4-8 is the sane range for a handful of
# external image CDNs from a single laptop.
BUILD_FETCH_BATCH_SIZE = 48


def _build_fetch_max_workers() -> int:
    return env_int("JBA_IMAGE_SEARCH_BUILD_WORKERS", 6, 4, 8)


class ImageSearchError(ValueError):
    """User-facing bad-input error (format/size/decoding)."""


class ModelNotReadyError(RuntimeError):
    """ONNX model file missing -- run scripts/prepare_image_search_model.py."""


class IndexNotReadyError(RuntimeError):
    """No index has been built yet."""


class IndexCorruptError(RuntimeError):
    """Index files exist but failed to load."""


@dataclass(frozen=True)
class SearchHit:
    product_id: int
    similarity: float


@dataclass
class BuildResult:
    total_products: int
    indexed_products: int
    skipped_no_image: int
    failed_image_download: int
    duration_seconds: float


# ---------------------------------------------------------------------------
# Model: lazy-loaded, process-wide singleton
# ---------------------------------------------------------------------------

_model_lock = threading.Lock()
_model_session = None


def is_model_ready() -> bool:
    return MODEL_PATH.is_file()


def _load_model():
    global _model_session
    if _model_session is not None:
        return _model_session
    with _model_lock:
        if _model_session is not None:
            return _model_session
        if not MODEL_PATH.is_file():
            raise ModelNotReadyError(
                f"图片搜索模型文件不存在：{MODEL_PATH}。请先运行 scripts/prepare_image_search_model.py"
            )
        import onnxruntime as ort
        _model_session = ort.InferenceSession(str(MODEL_PATH), providers=["CPUExecutionProvider"])
        return _model_session


def _preprocess(image: Image.Image) -> np.ndarray:
    image = image.convert("RGB")
    w, h = image.size
    scale = IMAGE_SIZE / min(w, h)
    new_w, new_h = max(1, round(w * scale)), max(1, round(h * scale))
    image = image.resize((new_w, new_h), Image.BICUBIC)
    left, top = (new_w - IMAGE_SIZE) // 2, (new_h - IMAGE_SIZE) // 2
    image = image.crop((left, top, left + IMAGE_SIZE, top + IMAGE_SIZE))
    arr = np.asarray(image, dtype=np.float32) / 255.0
    arr = (arr - IMAGE_MEAN) / IMAGE_STD
    return arr.transpose(2, 0, 1)


def embed_images(images: list[Image.Image]) -> np.ndarray:
    """Embed one or more PIL images -> L2-normalized (n, dim) float32 array."""
    session = _load_model()
    input_name = session.get_inputs()[0].name
    batch = np.stack([_preprocess(img) for img in images]).astype(np.float32)
    out = session.run(None, {input_name: batch})[0]
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return (out / norms).astype(np.float32)


def validate_query_image(content: bytes) -> Image.Image:
    """Validate & decode an uploaded query image. Raises ImageSearchError on any problem."""
    if not content:
        raise ImageSearchError("图片不能为空")
    if len(content) > MAX_QUERY_IMAGE_BYTES:
        raise ImageSearchError(f"图片不能超过 {MAX_QUERY_IMAGE_BYTES // 1024 // 1024}MB")
    try:
        with Image.open(io.BytesIO(content)) as opened:
            image_format = (opened.format or "").upper()
            opened.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageSearchError("请上传 JPG/PNG/WebP 图片") from exc
    if image_format not in ALLOWED_QUERY_FORMATS:
        raise ImageSearchError("请上传 JPG/PNG/WebP 图片")
    try:
        with Image.open(io.BytesIO(content)) as reopened:
            reopened.load()
            return reopened.convert("RGB").copy()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageSearchError("图片已损坏或无法解码，请重新拍摄/选择") from exc


# ---------------------------------------------------------------------------
# Index: lazy-loaded, process-wide singleton, reloaded after a rebuild
# ---------------------------------------------------------------------------

@dataclass
class _LoadedIndex:
    faiss_index: object
    product_ids: list[int]
    mtime: float


_index_lock = threading.Lock()
_loaded_index: _LoadedIndex | None = None


def index_files_exist() -> bool:
    return INDEX_PATH.is_file() and MAP_PATH.is_file() and META_PATH.is_file()


def read_index_meta() -> dict | None:
    if not META_PATH.is_file():
        return None
    try:
        return json.loads(META_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_index_locked() -> _LoadedIndex:
    if not index_files_exist():
        raise IndexNotReadyError("图片搜索索引尚未建立，请先由管理员重建索引。")
    try:
        import faiss
        faiss_index = faiss.read_index(str(INDEX_PATH))
        product_ids = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        if not isinstance(product_ids, list):
            raise ValueError("product_image_map.json 格式错误")
    except Exception as exc:
        raise IndexCorruptError("图片搜索索引文件已损坏，请由管理员重建索引。") from exc
    return _LoadedIndex(faiss_index=faiss_index, product_ids=product_ids, mtime=INDEX_PATH.stat().st_mtime)


def _get_loaded_index() -> _LoadedIndex:
    global _loaded_index
    current_mtime = INDEX_PATH.stat().st_mtime if INDEX_PATH.is_file() else None
    cached = _loaded_index
    if cached is not None and current_mtime == cached.mtime:
        return cached
    with _index_lock:
        current_mtime = INDEX_PATH.stat().st_mtime if INDEX_PATH.is_file() else None
        cached = _loaded_index
        if cached is not None and current_mtime == cached.mtime:
            return cached
        loaded = _load_index_locked()
        _loaded_index = loaded
        return loaded


def search_similar_products(image: Image.Image, top_k: int = TOP_K_DEFAULT) -> list[SearchHit]:
    """Never decides "the" match -- always returns a ranked candidate list for a human to pick from."""
    top_k = max(1, min(top_k, TOP_K_MAX))
    loaded = _get_loaded_index()
    vec = embed_images([image])
    scores, indices = loaded.faiss_index.search(vec, top_k)
    hits: list[SearchHit] = []
    for score, idx in zip(scores[0], indices[0]):
        if idx < 0 or idx >= len(loaded.product_ids):
            continue
        hits.append(SearchHit(product_id=loaded.product_ids[idx], similarity=float(score)))
    return hits


# ---------------------------------------------------------------------------
# Index building
# ---------------------------------------------------------------------------

_build_lock = threading.Lock()
_build_in_progress = False


def is_build_in_progress() -> bool:
    return _build_in_progress


def try_reserve_build_slot() -> bool:
    """Returns True if the caller may proceed with a build (slot now reserved),
    False if a build is already running elsewhere."""
    global _build_in_progress
    with _build_lock:
        if _build_in_progress:
            return False
        _build_in_progress = True
        return True


def _release_build_slot() -> None:
    global _build_in_progress
    with _build_lock:
        _build_in_progress = False


def run_index_build_job(engine: Engine) -> None:
    """BackgroundTasks entry point. Caller must have already reserved the build slot."""
    try:
        result = build_product_image_search_index(engine)
        logger.info(
            "image_search_index build完成 total=%s indexed=%s skipped_no_image=%s failed_download=%s duration=%.1fs",
            result.total_products, result.indexed_products, result.skipped_no_image,
            result.failed_image_download, result.duration_seconds,
        )
    except Exception:
        logger.exception("image_search_index build失败")
    finally:
        _release_build_slot()


def build_product_image_search_index(engine: Engine) -> BuildResult:
    """First-version index: one image per Product (its current "best" display
    image). Products with no usable image are skipped, not counted as
    failures. A single image fetch/decode failure only drops that one
    product -- it never aborts the whole build.

    Reads the Product list, releases the DB session, *then* does all
    network/embedding work -- no DB transaction is held across downloads.
    """
    started = time.perf_counter()
    SessionMaker = sessionmaker(bind=engine, expire_on_commit=False)

    with SessionMaker() as session:
        products = session.scalars(select(Product)).all()
        candidates = []
        for product in products:
            display = product_display_image(product)
            if display.status == "placeholder":
                continue
            candidates.append({
                "product_id": product.id,
                "status": display.status,
                "source_field": display.source_field,
                "local_image_path": product.local_image_path,
                "main_image_path": product.main_image_path,
                "remote_url": display.display_image_url if display.status == "remote" else None,
            })

    total_products = len(products)
    skipped_no_image = total_products - len(candidates)
    failed_image_download = 0

    import httpx
    vectors: list[np.ndarray] = []
    product_ids: list[int] = []
    max_workers = _build_fetch_max_workers()

    with httpx.Client(
        timeout=httpx.Timeout(BUILD_DOWNLOAD_TIMEOUT_SECONDS),
        follow_redirects=False,
        headers={"Accept": "image/jpeg,image/png,image/webp,image/gif", "User-Agent": "JBA/1.0"},
    ) as http_client:
        # Fetch (network download or local read) is I/O-bound and safe to run
        # concurrently across a small worker pool -- each item touches its own
        # bytes, no shared mutable state. Embedding stays single-threaded
        # (ONNX Runtime's own internal threading already uses the CPU) and
        # only ever sees one decoded batch at a time, so at most
        # BUILD_FETCH_BATCH_SIZE full-size images are ever resident in memory.
        for batch_start in range(0, len(candidates), BUILD_FETCH_BATCH_SIZE):
            batch = candidates[batch_start:batch_start + BUILD_FETCH_BATCH_SIZE]
            batch_images: list[Image.Image] = []
            batch_ids: list[int] = []
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_to_item = {
                    pool.submit(_fetch_candidate_image_bytes, item, http_client): item
                    for item in batch
                }
                for future in as_completed(future_to_item):
                    item = future_to_item[future]
                    try:
                        content = future.result()
                    except Exception as exc:
                        logger.warning("image_search_index build: 图片获取失败 product_id=%s err=%s", item["product_id"], exc)
                        content = None
                    if content is None:
                        failed_image_download += 1
                        continue
                    try:
                        with Image.open(io.BytesIO(content)) as im:
                            im.load()
                            pil_image = im.convert("RGB").copy()
                    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
                        logger.warning("image_search_index build: 图片解码失败 product_id=%s err=%s", item["product_id"], exc)
                        failed_image_download += 1
                        continue
                    batch_images.append(pil_image)
                    batch_ids.append(item["product_id"])
            if batch_images:
                vectors.append(embed_images(batch_images))
                product_ids.extend(batch_ids)
            # batch_images/batch_ids go out of scope here -- freed before the
            # next batch's fetches start, bounding peak memory to ~one batch.

    indexed_products = len(product_ids)
    if indexed_products == 0:
        raise RuntimeError("没有可用商品图片，索引未生成（旧索引未被改动）")

    all_vectors = np.concatenate(vectors, axis=0).astype(np.float32)

    import faiss
    faiss_index = faiss.IndexFlatIP(all_vectors.shape[1])
    faiss_index.add(all_vectors)

    duration_seconds = time.perf_counter() - started
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "model_name": MODEL_NAME,
        "embedding_dim": int(all_vectors.shape[1]),
        "product_count": total_products,
        "image_count": indexed_products,
        "skipped_no_image": skipped_no_image,
        "failed_image_download": failed_image_download,
        "duration_seconds": duration_seconds,
    }
    _atomic_write_index(faiss_index, product_ids, meta)

    with _index_lock:
        global _loaded_index
        _loaded_index = None  # force reload from disk on next search

    return BuildResult(
        total_products=total_products, indexed_products=indexed_products,
        skipped_no_image=skipped_no_image, failed_image_download=failed_image_download,
        duration_seconds=duration_seconds,
    )


def _fetch_candidate_image_bytes(item: dict, http_client) -> bytes | None:
    """Runs in a worker thread. Local reads and remote downloads both touch
    only this item's own data -- safe to call concurrently for different
    items sharing the same httpx.Client (httpx clients are thread-safe)."""
    if item["status"] == "local":
        return _resolve_local_image_bytes_from_dict(item)
    return download_remote_image(item["remote_url"], client=http_client).content


def _resolve_local_image_bytes_from_dict(item: dict) -> bytes | None:
    field = item.get("source_field")
    value = item.get(field) if field in ("local_image_path", "main_image_path") else None
    if not value or not _stored_product_image_exists(value):
        return None
    path = Path(value)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    resolved = path.resolve()
    allowed_roots = (PRODUCT_IMAGE_DIR.resolve(), QINSI_PRODUCT_IMAGE_DIR.resolve())
    if not any(resolved.is_relative_to(root) for root in allowed_roots):
        return None
    try:
        return resolved.read_bytes()
    except OSError:
        return None


def _atomic_write_index(faiss_index, product_ids: list[int], meta: dict) -> None:
    """Write to temp files, then atomically replace the live index -- a build
    that fails before this point never touches the existing live index."""
    import faiss
    IMAGE_SEARCH_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    tmp_index_path = IMAGE_SEARCH_DIR / f".products.faiss.tmp-{token}"
    tmp_map_path = IMAGE_SEARCH_DIR / f".product_image_map.json.tmp-{token}"
    tmp_meta_path = IMAGE_SEARCH_DIR / f".index_meta.json.tmp-{token}"
    try:
        faiss.write_index(faiss_index, str(tmp_index_path))
        tmp_map_path.write_text(json.dumps(product_ids), encoding="utf-8")
        tmp_meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_index_path, INDEX_PATH)
        os.replace(tmp_map_path, MAP_PATH)
        os.replace(tmp_meta_path, META_PATH)
    finally:
        for tmp_path in (tmp_index_path, tmp_map_path, tmp_meta_path):
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
