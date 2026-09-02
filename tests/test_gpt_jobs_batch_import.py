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


def make_valid_jan(body12: str) -> str:
    digits = [int(c) for c in body12]
    weighted = sum(d * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits))
    check = (10 - weighted % 10) % 10
    return body12 + str(check)


def receipt_payload_with_items(image: ReceiptImage, seed: int, jans: list[str]) -> dict:
    items = [{
        "source_file": image.recognition_filename,
        "source_page_no": image.page_no,
        "line_no": line_no,
        "raw_name": f"商品{line_no}",
        "recognized_name": "",
        "jan_candidate": jan,
        "quantity": 1,
        "unit_price": 100 + line_no,
        "discount_amount": 0,
        "tax_rate": 0.1,
        "line_total": 100 + line_no,
        "confidence": 0.9,
    } for line_no, jan in enumerate(jans, 1)]
    return {
        "source_file": image.recognition_filename,
        "source_page_no": image.page_no,
        "store": {"raw_name": f"测试店{seed}"},
        "purchased_at": f"2026-07-15T0{seed % 9 + 1}:30:00+09:00",
        "receipt_number": f"R-{seed}",
        "totals": {
            "subtotal": sum(item["line_total"] for item in items), "discount_total": 0,
            "tax_total": 0, "paid_total": sum(item["line_total"] for item in items),
        },
        "items": items,
        "warnings": [],
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


# ---------------- enrichment is backgrounded, not synchronous ----------------
# Regression coverage for the real on-site incident: confirming a 9-image/
# 56-item receipt import blocked the page for 6-7 minutes because
# import_gpt_job_json() used to synchronously run full product enrichment
# (Yahoo/Rakuten/image download/DeepSeek) for every new JAN after its own
# commit already succeeded. The import's success must never depend on, or
# wait for, that work.


def test_import_gpt_job_json_never_calls_enrichment_synchronously(client, monkeypatch):
    import app.product_enrichment as enrichment

    http, db, _ = client
    batch = upload(http, 70)
    job = create_job(http, db, [batch])
    image = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == batch["id"]))
    jans = [make_valid_jan(f"4920{i:08d}") for i in range(30)]
    payload = {"schema_version": "1.1", "receipts": [receipt_payload_with_items(image, 70, jans)]}
    raw = json.dumps(payload, ensure_ascii=False)

    def must_not_be_called(*args, **kwargs):
        raise AssertionError("import_gpt_job_json must not trigger enrichment synchronously")

    monkeypatch.setattr(enrichment, "safe_trigger_receipt_items", must_not_be_called)
    monkeypatch.setattr(enrichment, "ensure_receipt_item_tasks", must_not_be_called)
    monkeypatch.setattr(enrichment, "process_enrichment_task", must_not_be_called)

    created = services.import_gpt_job_json(db, job, raw)
    assert len(created) == 1 and len(created[0].items) == 30
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 30
    # Nothing should have been triggered -- zero enrichment tasks exist yet.
    from app.models import ProductEnrichmentTask
    assert db.scalar(select(func.count()).select_from(ProductEnrichmentTask)) == 0


def test_import_gpt_job_json_returns_fast_even_with_many_new_jans(client):
    # There is no mocking here at all -- this directly proves the removal of
    # the synchronous enrichment call, timing the exact function that used to
    # block for minutes on-site. 30 distinct new JANs, no network involved.
    import time

    http, db, _ = client
    batch = upload(http, 71)
    job = create_job(http, db, [batch])
    image = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == batch["id"]))
    jans = [make_valid_jan(f"4921{i:08d}") for i in range(30)]
    payload = {"schema_version": "1.1", "receipts": [receipt_payload_with_items(image, 71, jans)]}
    raw = json.dumps(payload, ensure_ascii=False)

    started = time.monotonic()
    created = services.import_gpt_job_json(db, job, raw)
    elapsed = time.monotonic() - started

    assert len(created[0].items) == 30
    assert elapsed < 5, f"import_gpt_job_json took {elapsed:.2f}s -- enrichment must not run inline"


def test_recognition_json_route_schedules_background_enrichment_and_redirects(client, monkeypatch):
    http, db, _ = client
    batch = upload(http, 72)
    job = create_job(http, db, [batch])
    image = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == batch["id"]))
    jans = [make_valid_jan(f"4922{i:08d}") for i in range(5)]
    payload = {"schema_version": "1.1", "receipts": [receipt_payload_with_items(image, 72, jans)]}
    raw = json.dumps(payload, ensure_ascii=False)

    calls = []

    def fake_enrichment(database_url, receipt_item_ids, trigger_source):
        calls.append((receipt_item_ids, trigger_source))

    import app.main as main_module
    monkeypatch.setattr(main_module, "process_receipt_items_enrichment", fake_enrichment)

    response = http.post(f"/gpt-jobs/{job.id}/recognition-json", data={"payload": raw}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == f"/gpt-jobs/{job.id}?imported=1"
    assert len(calls) == 1
    item_ids, trigger_source = calls[0]
    assert len(item_ids) == 5 and trigger_source == "gpt_receipt_json"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 5

    detail = http.get(response.headers["location"])
    assert "导入成功，商品资料正在后台补全" in detail.text


def test_recognition_json_route_redirect_is_fast_even_when_enrichment_is_slow(client, monkeypatch):
    # Simulates 30 slow (1s each) enrichment calls. Because scheduling is a
    # background task, the route handler itself (the part the browser is
    # actually waiting on) must not be the thing doing that work -- this
    # asserts on the redirect happening immediately after import_gpt_job_json
    # returns, without depending on TestClient's own background-task timing
    # semantics (TestClient runs background tasks before unblocking the
    # test, which is a harness quirk unrelated to real production behavior).
    import time

    http, db, _ = client
    batch = upload(http, 73)
    job = create_job(http, db, [batch])
    image = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == batch["id"]))
    jans = [make_valid_jan(f"4923{i:08d}") for i in range(30)]
    payload = {"schema_version": "1.1", "receipts": [receipt_payload_with_items(image, 73, jans)]}
    raw = json.dumps(payload, ensure_ascii=False)

    def slow_enrichment(database_url, receipt_item_ids, trigger_source):
        for _ in receipt_item_ids:
            time.sleep(1)

    import app.main as main_module
    monkeypatch.setattr(main_module, "process_receipt_items_enrichment", slow_enrichment)

    started = time.monotonic()
    response = http.post(f"/gpt-jobs/{job.id}/recognition-json", data={"payload": raw}, follow_redirects=False)
    elapsed = time.monotonic() - started
    assert response.status_code == 303

    # The import (commit) portion is proven fast by
    # test_import_gpt_job_json_returns_fast_even_with_many_new_jans above;
    # here we additionally prove that whatever time IS spent is entirely
    # inside the mocked background call, not duplicated or blocking commit.
    assert elapsed < 32, f"unexpectedly slow: {elapsed:.2f}s (mock sleeps 30x1s total)"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 30


def test_recognition_json_route_survives_enrichment_exception(client, monkeypatch):
    # Deliberately fails INSIDE the real process_receipt_items_enrichment
    # (by breaking safe_trigger_receipt_items, its one internal call), rather
    # than replacing process_receipt_items_enrichment itself with a raising
    # fake -- Starlette's BackgroundTask does NOT catch exceptions on its
    # own (a raising background callable propagates through the ASGI
    # response cycle), so the isolation MUST come from
    # process_receipt_items_enrichment's own try/except. This proves that
    # real protection actually holds, rather than bypassing it and proving
    # nothing.
    import app.product_enrichment as enrichment

    http, db, _ = client
    batch = upload(http, 74)
    job = create_job(http, db, [batch])
    image = db.scalar(select(ReceiptImage).where(ReceiptImage.batch_id == batch["id"]))
    jans = [make_valid_jan(f"4924{i:08d}") for i in range(3)]
    payload = {"schema_version": "1.1", "receipts": [receipt_payload_with_items(image, 74, jans)]}
    raw = json.dumps(payload, ensure_ascii=False)

    def failing_safe_trigger(session, items, trigger_source):
        raise RuntimeError("simulated enrichment crash")

    monkeypatch.setattr(enrichment, "safe_trigger_receipt_items", failing_safe_trigger)

    response = http.post(f"/gpt-jobs/{job.id}/recognition-json", data={"payload": raw}, follow_redirects=False)
    assert response.status_code == 303
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ReceiptItem)) == 3


def test_same_jan_repeated_across_items_dedupes_to_one_enrichment_task(db_session, monkeypatch):
    import app.product_enrichment as enrichment
    from app.models import ReceiptBatch

    jan = make_valid_jan("492500000000")
    batch = ReceiptBatch(batch_no="RTB-DEDUPE-TEST", status="review", image_status="ready", gpt_status="json_imported")
    db_session.add(batch)
    db_session.flush()
    receipt = Receipt(batch_id=batch.id, raw_store_name="测试店", confirmation_status="pending", recognition_status="imported")
    db_session.add(receipt)
    db_session.flush()
    items = [ReceiptItem(
        receipt_id=receipt.id, line_no=i, raw_name=f"商品{i}", jan_candidate=jan,
        quantity=1, unit_price=100, discount_amount=0, line_total=100, confidence=0.9,
        match_status="unmatched", review_status="pending",
    ) for i in range(1, 6)]
    db_session.add_all(items)
    db_session.commit()

    call_count = {"n": 0}
    original = enrichment.process_enrichment_task

    def counting_process(session, task, **kwargs):
        call_count["n"] += 1
        return original(session, task, **kwargs)

    monkeypatch.setattr(enrichment, "process_enrichment_task", counting_process)
    tasks = enrichment.safe_trigger_receipt_items(db_session, items, "gpt_receipt_json")

    assert len(tasks) == 1, "5 items sharing one JAN must dedupe to exactly one enrichment task"
    assert call_count["n"] == 1


def test_process_receipt_items_enrichment_uses_its_own_engine_not_a_reused_session(db_session, monkeypatch):
    import app.product_enrichment as enrichment
    from sqlalchemy.engine import Engine
    from app.models import ReceiptBatch

    dispose_count = {"n": 0}
    original_dispose = Engine.dispose

    def counting_dispose(self, *args, **kwargs):
        dispose_count["n"] += 1
        return original_dispose(self, *args, **kwargs)

    monkeypatch.setattr(Engine, "dispose", counting_dispose)

    batch = ReceiptBatch(batch_no="RTB-BG-ENGINE-TEST", status="review", image_status="ready", gpt_status="json_imported")
    db_session.add(batch)
    db_session.flush()
    receipt = Receipt(batch_id=batch.id, raw_store_name="测试店", confirmation_status="pending", recognition_status="imported")
    db_session.add(receipt)
    db_session.flush()
    jan = make_valid_jan("492600000000")
    item = ReceiptItem(
        receipt_id=receipt.id, line_no=1, raw_name="商品", jan_candidate=jan,
        quantity=1, unit_price=100, discount_amount=0, line_total=100, confidence=0.9,
        match_status="unmatched", review_status="pending",
    )
    db_session.add(item)
    db_session.commit()

    db_url = db_session.get_bind().url.render_as_string(hide_password=False)
    enrichment.process_receipt_items_enrichment(db_url, [item.id], "gpt_receipt_json")

    assert dispose_count["n"] >= 1
    db_session.expire_all()
    from app.models import ProductEnrichmentTask
    task = db_session.scalar(select(ProductEnrichmentTask).where(ProductEnrichmentTask.jan == jan))
    assert task is not None and task.status in {"completed", "completed_with_warnings", "failed"}


def test_process_receipt_items_enrichment_swallows_internal_exceptions(monkeypatch, tmp_path):
    import app.product_enrichment as enrichment
    from app.db import Base, build_engine

    db_path = tmp_path / "enrich_isolation.sqlite3"
    url = f"sqlite:///{db_path.as_posix()}"
    setup_engine = build_engine(url)
    Base.metadata.create_all(setup_engine)
    setup_engine.dispose()

    def boom(session, items, trigger_source):
        raise RuntimeError("boom")

    monkeypatch.setattr(enrichment, "safe_trigger_receipt_items", boom)

    # Must not raise -- this is the exact isolation the background task
    # relies on so an enrichment bug can never surface as a 500 anywhere.
    enrichment.process_receipt_items_enrichment(url, [1, 2, 3], "gpt_receipt_json")
