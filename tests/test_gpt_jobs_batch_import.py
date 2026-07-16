from __future__ import annotations

from copy import deepcopy
import io
import json
import random

import pytest
from PIL import Image, ImageDraw
from pydantic import ValidationError
from sqlalchemy import func, select

import app.services as services
from app.models import AiRecognitionRun, Receipt, ReceiptBatch, ReceiptImage, ReceiptItem, ZipPackageJob
from app.schemas import RecognitionBatchInput


def picture(seed: int) -> bytes:
    rng = random.Random(seed)
    image = Image.new("RGB", (180, 260), (235 + seed % 17, 220 + seed % 29, 205 + seed % 37))
    draw = ImageDraw.Draw(image)
    draw.text((15, 20), f"RECEIPT {seed:04d}", fill=(seed * 13 % 255, 20, 20))
    for index in range(24):
        x1, y1 = rng.randrange(5, 150), rng.randrange(45, 245)
        x2, y2 = min(175, x1 + rng.randrange(5, 45)), min(255, y1 + rng.randrange(2, 20))
        draw.rectangle((x1, y1, x2, y2), fill=(rng.randrange(256), rng.randrange(256), rng.randrange(256)))
    output = io.BytesIO()
    image.save(output, "JPEG", quality=93)
    return output.getvalue()


def upload(http, seed: int) -> dict:
    response = http.post(
        "/api/receipt-batches/upload",
        files={"files": (f"receipt-{seed}.jpg", picture(seed), "image/jpeg")},
        data={"source_type": "mobile", "request_id": f"gpt-job-test-{seed:04d}"},
    )
    assert response.status_code == 201
    return response.json()


def create_job(http, db, batches: list[dict]) -> ZipPackageJob:
    query = "&".join(f"batch_ids={batch['id']}" for batch in batches)
    response = http.get(f"/receipts/recognition-images.zip?{query}")
    assert response.status_code == 200
    db.expire_all()
    return db.scalar(select(ZipPackageJob).order_by(ZipPackageJob.id.desc()))


def receipt_payload(image: ReceiptImage, seed: int = 1) -> dict:
    return {
        "source_file": image.recognition_filename,
        "source_page_no": image.page_no,
        "store": {"raw_name": f"测试店{seed}"},
        "purchased_at": f"2026-07-15T0{seed}:30:00+09:00",
        "receipt_number": f"R-{seed}",
        "totals": {"subtotal": 1000 + seed, "discount_total": 0, "tax_total": 91, "paid_total": 1000 + seed},
        "items": [{
            "source_file": image.recognition_filename,
            "source_page_no": image.page_no,
            "line_no": 1,
            "raw_name": f"商品{seed}",
            "recognized_name": "",
            "jan_candidate": None,
            "quantity": 1,
            "unit_price": 1000 + seed,
            "discount_amount": 0,
            "tax_rate": 0.1,
            "line_total": 1000 + seed,
            "confidence": 0.9,
        }],
        "warnings": ["测试警告"] if seed == 2 else [],
    }


def batch_payload(db, batches: list[dict]) -> dict:
    receipts = []
    for seed, batch in enumerate(batches, 1):
        image = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == batch["id"]))
        receipts.append(receipt_payload(image, seed))
    return {"schema_version": "1.1", "receipts": receipts}


def test_schema_11_batch_validation_and_legacy_10_job_compatibility(client, valid_payload):
    http, db, _ = client
    batches = [upload(http, 1), upload(http, 2)]
    payload = batch_payload(db, batches)
    parsed = RecognitionBatchInput.model_validate(payload)
    assert len(parsed.receipts) == 2

    single = create_job(http, db, [batches[0]])
    preview = services.preview_gpt_job_import(db, single, json.dumps(valid_payload, ensure_ascii=False))
    assert preview.can_import and preview.matched_image_count == 1


def test_multibatch_preview_exact_matches_and_writes_nothing(client):
    http, db, _ = client
    batches = [upload(http, seed) for seed in (3, 4, 5)]
    job = create_job(http, db, batches)
    payload = batch_payload(db, batches)
    before = (db.scalar(select(func.count()).select_from(Receipt)), db.scalar(select(func.count()).select_from(AiRecognitionRun)))
    response = http.post(f"/gpt-jobs/{job.id}/recognition-preview", data={"payload": json.dumps(payload, ensure_ascii=False)})
    assert response.status_code == 200
    assert "匹配成功图片" in response.text and ">3<" in response.text and "确认导入" in response.text
    after = (db.scalar(select(func.count()).select_from(Receipt)), db.scalar(select(func.count()).select_from(AiRecognitionRun)))
    assert before == after == (0, 0)


@pytest.mark.parametrize("problem", ["unknown", "duplicate", "page", "item_source", "missing"])
def test_preview_rejects_invalid_or_incomplete_sources(client, problem):
    http, db, _ = client
    batches = [upload(http, 10), upload(http, 11)]
    job = create_job(http, db, batches)
    payload = batch_payload(db, batches)
    if problem == "unknown":
        payload["receipts"][0]["source_file"] = "RCPT-20260715-9999-NONE_P01.jpg"
        payload["receipts"][0]["items"][0]["source_file"] = payload["receipts"][0]["source_file"]
    elif problem == "duplicate":
        payload["receipts"][1] = deepcopy(payload["receipts"][0])
    elif problem == "page":
        payload["receipts"][0]["source_page_no"] = 9
        payload["receipts"][0]["items"][0]["source_page_no"] = 9
    elif problem == "item_source":
        payload["receipts"][0]["items"][0]["source_file"] = payload["receipts"][1]["source_file"]
    else:
        payload["receipts"].pop()
    raw = json.dumps(payload, ensure_ascii=False)
    if problem == "item_source":
        with pytest.raises(ValueError, match="item.source_file"):
            services.preview_gpt_job_import(db, job, raw)
    else:
        preview = services.preview_gpt_job_import(db, job, raw)
        assert not preview.can_import
        with pytest.raises(ValueError):
            services.import_gpt_job_json(db, job, raw)
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0


def test_cross_job_source_file_is_rejected(client):
    http, db, _ = client
    first, second = upload(http, 20), upload(http, 21)
    first_job = create_job(http, db, [first])
    create_job(http, db, [second])
    foreign = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == second["id"]))
    payload = {"schema_version": "1.1", "receipts": [receipt_payload(foreign)]}
    preview = services.preview_gpt_job_import(db, first_job, json.dumps(payload))
    assert preview.cross_job_source_files == [foreign.recognition_filename]
    assert not preview.can_import


def test_atomic_import_saves_item_sources_statuses_and_history(client):
    http, db, _ = client
    batches = [upload(http, seed) for seed in (30, 31, 32)]
    job = create_job(http, db, batches)
    raw = json.dumps(batch_payload(db, batches), ensure_ascii=False)
    response = http.post(f"/gpt-jobs/{job.id}/recognition-json", data={"payload": raw}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    items = list(db.scalars(select(ReceiptItem)))
    assert len(items) == 3 and all(item.source_image_id for item in items)
    for item in items:
        assert db.get(ReceiptImage, item.source_image_id).batch_id == item.receipt.batch_id
    assert all(batch.gpt_status == "json_imported" for batch in db.scalars(select(ReceiptBatch)))
    current_job = db.get(ZipPackageJob, job.id)
    assert current_job.gpt_status == "review_pending" and current_job.json_imported_at
    runs = list(db.scalars(select(AiRecognitionRun)))
    assert len(runs) == 3 and all(run.raw_response_json == raw and run.zip_job_id == job.id for run in runs)

    assert http.post(f"/gpt-jobs/{job.id}/recognition-json", data={"payload": raw}, follow_redirects=False).status_code == 303
    assert db.scalar(select(func.count()).select_from(AiRecognitionRun)) == 6
    assert db.scalar(select(func.count()).select_from(Receipt)) == 3


def test_failure_rolls_back_all_batches_without_partial_results(client, monkeypatch):
    http, db, _ = client
    batches = [upload(http, 40), upload(http, 41)]
    job = create_job(http, db, batches)
    raw = json.dumps(batch_payload(db, batches))
    calls = 0
    original = services.detect_business_duplicate

    def fail_second(session, receipt):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("forced failure")
        return original(session, receipt)

    monkeypatch.setattr(services, "detect_business_duplicate", fail_second)
    with pytest.raises(RuntimeError, match="forced failure"):
        services.import_gpt_job_json(db, job, raw)
    assert db.scalar(select(func.count()).select_from(Receipt)) == 0
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 0
    assert db.scalar(select(func.count()).select_from(AiRecognitionRun)) == 0
    assert all(batch.gpt_status == "zip_downloaded" for batch in db.scalars(select(ReceiptBatch)))


def test_job_page_status_and_batch_detail_has_no_json_import_form(client):
    http, db, _ = client
    batch = upload(http, 50)
    job = create_job(http, db, [batch])
    marked = http.post(f"/gpt-jobs/{job.id}/sent-to-gpt", headers={"accept": "application/json"})
    assert marked.status_code == 200 and marked.json()["status_text"] == "✓ 已交给GPT · 等待JSON"
    page = http.get(f"/gpt-jobs/{job.id}").text
    assert "✓ 已交给GPT · 等待JSON" in page and "导入 JSON（先预览）" in page
    detail = http.get(f"/receipts/{batch['id']}").text
    assert "GPT JSON 人工导入" not in detail and f"/gpt-jobs/{job.id}" in detail


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("整理商品名", "整理商品名"), ("", None)])
def test_recognized_name_accepts_null_string_and_normalizes_blank(client, value, expected):
    http, db, _ = client
    batch = upload(http, 60)
    job = create_job(http, db, [batch])
    payload = batch_payload(db, [batch])
    payload["receipts"][0]["items"][0]["recognized_name"] = value
    preview = services.preview_gpt_job_import(db, job, json.dumps(payload, ensure_ascii=False))
    item = preview.parsed.receipts[0].items[0]
    assert preview.can_import and item.recognized_name == expected
    assert item.raw_name == "商品1"


def test_all_documented_nullable_fields_are_consistent(client):
    http, db, _ = client
    batch = upload(http, 61)
    job = create_job(http, db, [batch])
    payload = batch_payload(db, [batch])
    receipt = payload["receipts"][0]
    receipt.update(purchased_at=None, receipt_number=None)
    receipt["totals"].update(subtotal=None, tax_total=None, paid_total=None)
    receipt["items"][0].update(
        recognized_name=None,
        jan_candidate=None,
        unit_price=None,
        tax_rate=None,
        line_total=None,
    )
    preview = services.preview_gpt_job_import(db, job, json.dumps(payload))
    assert preview.can_import
    parsed = preview.parsed.receipts[0]
    assert parsed.purchased_at is parsed.receipt_number is None
    assert parsed.totals.subtotal is parsed.totals.tax_total is parsed.totals.paid_total is None
    assert parsed.items[0].recognized_name is parsed.items[0].jan_candidate is None
    assert parsed.items[0].unit_price is parsed.items[0].tax_rate is parsed.items[0].line_total is None


@pytest.mark.parametrize("invalid", [{"bad": "value"}, ["bad"]])
def test_recognized_name_rejects_objects_and_arrays(client, invalid):
    http, db, _ = client
    batch = upload(http, 62)
    create_job(http, db, [batch])
    payload = batch_payload(db, [batch])
    payload["receipts"][0]["items"][0]["recognized_name"] = invalid
    with pytest.raises(ValidationError):
        RecognitionBatchInput.model_validate(payload)


def test_validation_page_shows_only_five_chinese_summaries(client):
    http, db, _ = client
    batch = upload(http, 63)
    job = create_job(http, db, [batch])
    payload = batch_payload(db, [batch])
    base_item = payload["receipts"][0]["items"][0]
    payload["receipts"][0]["items"] = []
    for line_no in range(1, 7):
        item = deepcopy(base_item)
        item.update(line_no=line_no, recognized_name={"invalid": line_no})
        payload["receipts"][0]["items"].append(item)
    response = http.post(
        f"/gpt-jobs/{job.id}/recognition-preview",
        data={"payload": json.dumps(payload, ensure_ascii=False)},
    )
    assert response.status_code == 422
    assert response.text.count("整理后商品名格式错误") == 5
    assert "另有1条错误" in response.text
    assert "展开查看技术详情" in response.text


def test_schema_10_accepts_null_recognized_name(client, valid_payload):
    http, db, _ = client
    batch = upload(http, 64)
    job = create_job(http, db, [batch])
    valid_payload["items"][0]["recognized_name"] = None
    preview = services.preview_gpt_job_import(db, job, json.dumps(valid_payload, ensure_ascii=False))
    assert preview.can_import and preview.parsed.receipts[0].items[0].recognized_name is None
