from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ReceiptBatch(Base):
    __tablename__ = "receipt_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    request_id: Mapped[str | None] = mapped_column(String(100), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)
    current_stage: Mapped[str] = mapped_column(String(30), default="uploaded", nullable=False)
    upload_errors_json: Mapped[str | None] = mapped_column(Text)
    image_status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)
    gpt_status: Mapped[str] = mapped_column(String(30), default="not_packaged", nullable=False)
    product_status: Mapped[str] = mapped_column(String(30), default="not_matched", nullable=False)
    qinsi_status: Mapped[str] = mapped_column(String(30), default="not_exported", nullable=False)
    zip_first_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zip_last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    zip_download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gpt_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    json_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    image_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source_type: Mapped[str] = mapped_column(String(20), default="unknown", nullable=False)
    recognition_engine: Mapped[str] = mapped_column(String(30), default="none", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    images: Mapped[list[ReceiptImage]] = relationship(back_populates="batch", cascade="all, delete-orphan", passive_deletes=True, order_by="ReceiptImage.page_no")
    receipts: Mapped[list[Receipt]] = relationship(back_populates="batch", cascade="all, delete-orphan", passive_deletes=True)
    recognition_runs: Mapped[list[AiRecognitionRun]] = relationship(back_populates="batch", cascade="all, delete-orphan", passive_deletes=True)


class ReceiptImage(Base):
    __tablename__ = "receipt_images"
    __table_args__ = (UniqueConstraint("batch_id", "page_no", name="uq_receipt_image_batch_page"), Index("ix_receipt_images_file_hash", "file_hash"))
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("receipt_batches.id", ondelete="CASCADE"), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    recognition_filename: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    stored_filename: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    original_path: Mapped[str] = mapped_column(Text, nullable=False)
    processed_path: Mapped[str | None] = mapped_column(Text)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    perceptual_hash: Mapped[str | None] = mapped_column(String(100), index=True)
    normalized_image_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    duplicate_of_image_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_images.id", ondelete="SET NULL"), index=True)
    duplicate_score: Mapped[float | None] = mapped_column(Float)
    duplicate_status: Mapped[str] = mapped_column(String(30), default="none", nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    preprocessing_status: Mapped[str] = mapped_column(String(30), default="previewed", nullable=False)
    processing_method: Mapped[str | None] = mapped_column(String(100))
    processing_warning: Mapped[str | None] = mapped_column(Text)
    processed_width: Mapped[int | None] = mapped_column(Integer)
    processed_height: Mapped[int | None] = mapped_column(Integer)
    recognition_source: Mapped[str] = mapped_column(String(20), default="processed", nullable=False)
    rotation_degrees: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    batch: Mapped[ReceiptBatch] = relationship(back_populates="images")


class Receipt(Base):
    __tablename__ = "receipts"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("receipt_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    source_image_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_images.id", ondelete="SET NULL"), index=True)
    raw_store_name: Mapped[str | None] = mapped_column(String(255))
    raw_store_code: Mapped[str | None] = mapped_column(String(100))
    raw_store_phone: Mapped[str | None] = mapped_column(String(50))
    raw_store_postal_code: Mapped[str | None] = mapped_column(String(20))
    raw_store_address: Mapped[str | None] = mapped_column(Text)
    raw_store_branch_name: Mapped[str | None] = mapped_column(String(255))
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), index=True)
    store_match_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    store_match_method: Mapped[str | None] = mapped_column(String(30))
    store_match_confidence: Mapped[float | None] = mapped_column(Float)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    receipt_number: Mapped[str | None] = mapped_column(String(100))
    subtotal: Mapped[int | None] = mapped_column(Integer)
    discount_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_total: Mapped[int | None] = mapped_column(Integer)
    paid_total: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="JPY", nullable=False)
    recognition_status: Mapped[str] = mapped_column(String(20), default="imported", nullable=False)
    confirmation_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmation_warning: Mapped[str | None] = mapped_column(Text)
    business_fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    duplicate_of_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id", ondelete="SET NULL"), index=True)
    duplicate_score: Mapped[float | None] = mapped_column(Float)
    duplicate_status: Mapped[str] = mapped_column(String(30), default="none", nullable=False, index=True)
    duplicate_reason: Mapped[str | None] = mapped_column(Text)
    batch: Mapped[ReceiptBatch] = relationship(back_populates="receipts")
    items: Mapped[list[ReceiptItem]] = relationship(back_populates="receipt", cascade="all, delete-orphan", passive_deletes=True, order_by="ReceiptItem.line_no")
    purchase_batch: Mapped[PurchaseBatch | None] = relationship(back_populates="receipt", uselist=False)
    store: Mapped[Store | None] = relationship(back_populates="receipts")


class ReceiptItem(Base):
    __tablename__ = "receipt_items"
    __table_args__ = (
        UniqueConstraint("receipt_id", "line_no", name="uq_receipt_item_receipt_line"),
        CheckConstraint("quantity > 0", name="ck_receipt_items_quantity_positive"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_receipt_items_confidence_range"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="CASCADE"), nullable=False)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_name: Mapped[str] = mapped_column(Text, nullable=False)
    recognized_name: Mapped[str | None] = mapped_column(Text)
    jan_candidate: Mapped[str | None] = mapped_column(String(32))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    match_status: Mapped[str] = mapped_column(String(30), default="unmatched", nullable=False)
    matched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    match_method: Mapped[str | None] = mapped_column(String(50))
    match_confidence: Mapped[float | None] = mapped_column(Float)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int | None] = mapped_column(Integer)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tax_rate: Mapped[float | None] = mapped_column(Float)
    line_total: Mapped[int | None] = mapped_column(Integer)
    confidence: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    review_status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    source_image_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_images.id", ondelete="SET NULL"))
    source_region_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    receipt: Mapped[Receipt] = relationship(back_populates="items")
    purchase_detail: Mapped[PurchaseBatchItem | None] = relationship(back_populates="receipt_item", uselist=False)


class AiRecognitionRun(Base):
    __tablename__ = "ai_recognition_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("receipt_batches.id", ondelete="CASCADE"), nullable=False)
    image_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_images.id", ondelete="SET NULL"))
    zip_job_id: Mapped[int | None] = mapped_column(ForeignKey("zip_package_jobs.id", ondelete="SET NULL"), index=True)
    provider: Mapped[str] = mapped_column(String(50), default="manual_chatgpt", nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(100))
    prompt_version: Mapped[str | None] = mapped_column(String(50))
    raw_response_json: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_json: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    batch: Mapped[ReceiptBatch] = relationship(back_populates="recognition_runs")
    zip_job: Mapped[ZipPackageJob | None] = relationship(back_populates="recognition_runs")


class DuplicateDetectionLog(Base):
    __tablename__ = "duplicate_detection_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    new_entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    matched_entity_id: Mapped[int | None] = mapped_column(Integer, index=True)
    algorithm_version: Mapped[str] = mapped_column(String(30), nullable=False)
    sha_match: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    perceptual_distance: Mapped[int | None] = mapped_column(Integer)
    business_score: Mapped[float | None] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ZipPackageJob(Base):
    __tablename__ = "zip_package_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_no: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    selection_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    batch_count: Mapped[int] = mapped_column(Integer, nullable=False)
    image_count: Mapped[int] = mapped_column(Integer, nullable=False)
    excluded_duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    first_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    download_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    gpt_status: Mapped[str] = mapped_column(String(30), default="zip_ready", nullable=False)
    gpt_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    json_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    items: Mapped[list[ZipPackageItem]] = relationship(back_populates="job", cascade="all, delete-orphan", passive_deletes=True, order_by="ZipPackageItem.id")
    recognition_runs: Mapped[list[AiRecognitionRun]] = relationship(back_populates="zip_job")


class ZipPackageItem(Base):
    __tablename__ = "zip_package_items"
    __table_args__ = (UniqueConstraint("job_id", "image_id", name="uq_zip_package_item_job_image"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("zip_package_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("receipt_batches.id", ondelete="RESTRICT"), nullable=False, index=True)
    image_id: Mapped[int] = mapped_column(ForeignKey("receipt_images.id", ondelete="RESTRICT"), nullable=False, index=True)
    recognition_filename: Mapped[str] = mapped_column(String(100), nullable=False)
    excluded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    job: Mapped[ZipPackageJob] = relationship(back_populates="items")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        Index("uq_products_internal_sku", "internal_sku", unique=True),
        Index("uq_products_jan_not_null", "jan", unique=True, sqlite_where=text("jan IS NOT NULL")),
        Index("uq_products_qinsi_code_not_null", "qinsi_product_code", unique=True, sqlite_where=text("qinsi_product_code IS NOT NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    internal_sku: Mapped[str] = mapped_column(String(32), nullable=False)
    jan: Mapped[str | None] = mapped_column(String(32))
    qinsi_product_code: Mapped[str | None] = mapped_column(String(100))
    name_cn: Mapped[str | None] = mapped_column(String(255))
    name_ja: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(257))
    name_source: Mapped[str | None] = mapped_column(String(30))
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    main_image_path: Mapped[str | None] = mapped_column(Text)
    main_image_source_url: Mapped[str | None] = mapped_column(Text)
    product_data_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    name_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    main_image_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    main_image_source_platform: Mapped[str | None] = mapped_column(String(50))
    main_image_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    main_image_hash: Mapped[str | None] = mapped_column(String(64))
    image_width: Mapped[int | None] = mapped_column(Integer)
    image_height: Mapped[int | None] = mapped_column(Integer)
    image_quality: Mapped[str | None] = mapped_column(String(20))
    brand: Mapped[str | None] = mapped_column(String(128))
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    capacity: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(64))
    model_number: Mapped[str | None] = mapped_column(String(128))
    package_count: Mapped[str | None] = mapped_column(String(64))
    specification: Mapped[str | None] = mapped_column(String(255))
    net_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    volume_ml: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    depth_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    pack_quantity: Mapped[int | None] = mapped_column(Integer)
    spec_text: Mapped[str | None] = mapped_column(Text)
    model_spec: Mapped[str | None] = mapped_column(String(255))
    purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    sale_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    minimum_sale_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    image_url: Mapped[str | None] = mapped_column(Text)
    display_image_url: Mapped[str | None] = mapped_column(Text)
    local_image_path: Mapped[str | None] = mapped_column(Text)
    image_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    image_localization_status: Mapped[str | None] = mapped_column(String(30), index=True)
    image_localization_source_url: Mapped[str | None] = mapped_column(Text)
    image_localized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    image_localization_error: Mapped[str | None] = mapped_column(Text)
    location_code: Mapped[str | None] = mapped_column(String(100))
    unit_name: Mapped[str | None] = mapped_column(String(128))
    qinsi_sort_order: Mapped[int | None] = mapped_column(Integer)
    qinsi_points_enabled: Mapped[bool | None] = mapped_column(Boolean)
    inventory_warning_lower: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    inventory_warning_upper: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    shelf_life_days: Mapped[int | None] = mapped_column(Integer)
    batch_enabled: Mapped[bool | None] = mapped_column(Boolean)
    expiration_warning_days: Mapped[int | None] = mapped_column(Integer)
    product_note: Mapped[str | None] = mapped_column(Text)
    origin_place: Mapped[str | None] = mapped_column(String(255))
    applicable_age: Mapped[str | None] = mapped_column(String(255))
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    serial_number_enabled: Mapped[bool | None] = mapped_column(Boolean)
    qinsi_brand_master_id: Mapped[int | None] = mapped_column(ForeignKey("qinsi_master_values.id", ondelete="SET NULL"))
    qinsi_category_master_id: Mapped[int | None] = mapped_column(ForeignKey("qinsi_master_values.id", ondelete="SET NULL"))
    qinsi_unit_master_id: Mapped[int | None] = mapped_column(ForeignKey("qinsi_master_values.id", ondelete="SET NULL"))
    qinsi_product_barcode: Mapped[str | None] = mapped_column(String(100))
    qinsi_unit_barcode: Mapped[str | None] = mapped_column(String(100))
    qinsi_name: Mapped[str | None] = mapped_column(String(255))
    qinsi_image_url: Mapped[str | None] = mapped_column(Text)
    qinsi_brand: Mapped[str | None] = mapped_column(String(128))
    qinsi_category: Mapped[str | None] = mapped_column(String(128))
    qinsi_unit: Mapped[str | None] = mapped_column(String(128))
    qinsi_status: Mapped[str | None] = mapped_column(String(50))
    qinsi_remark: Mapped[str | None] = mapped_column(Text)
    qinsi_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    has_jan: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="active", nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="manual", nullable=False)
    product_origin: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    low_stock_threshold: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    purchase_details: Mapped[list[PurchaseBatchItem]] = relationship(back_populates="product")
    price_search_runs: Mapped[list[PriceSearchRun]] = relationship(back_populates="product")
    watch_config: Mapped[ProductWatchConfig | None] = relationship(back_populates="product", uselist=False)
    watch_recommendations: Mapped[list[ProductWatchRecommendation]] = relationship(back_populates="product")
    watch_snapshots: Mapped[list[ProductWatchSnapshot]] = relationship(back_populates="product")
    watch_notifications: Mapped[list[ProductWatchNotification]] = relationship(back_populates="product")
    qinsi_inventory_lines: Mapped[list[QinsiInventorySnapshotLine]] = relationship(back_populates="product")
    qinsi_sales_summary_lines: Mapped[list[QinsiSalesSummaryLine]] = relationship(back_populates="product")
    qinsi_product_mappings: Mapped[list[QinsiProductMapping]] = relationship(back_populates="product")
    barcodes: Mapped[list[ProductBarcode]] = relationship(back_populates="product", cascade="all, delete-orphan")
    restock_list_items: Mapped[list[RestockListItem]] = relationship(back_populates="product")
    field_purchase_items: Mapped[list[FieldPurchaseItem]] = relationship(back_populates="product")
    serials: Mapped[list[ProductSerial]] = relationship(back_populates="product", cascade="all, delete-orphan")
    operation_logs: Mapped[list[ProductOperationLog]] = relationship(back_populates="product", order_by="ProductOperationLog.created_at.desc()")

    @property
    def preferred_image_url(self) -> str | None:
        if self.display_image_url:
            return self.display_image_url
        if self.main_image_path and self.id:
            return f"/product-images/{self.id}"
        return self.main_image_source_url or self.image_url

    @property
    def compact_spec(self) -> str:
        from app.product_specs import compact_spec_label

        return compact_spec_label(self)


class ProductAlias(Base):
    __tablename__ = "product_aliases"
    __table_args__ = (UniqueConstraint("product_id", "normalized_alias", name="uq_product_alias_product_normalized"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_from_item_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_items.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProductOperationLog(Base):
    __tablename__ = "product_operation_logs"
    __table_args__ = (
        CheckConstraint("action IN ('edit','archive','restore','delete')", name="ck_product_operation_logs_action"),
        Index("ix_product_operation_logs_product_created", "product_id", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    internal_sku: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str | None] = mapped_column(Text)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    product: Mapped[Product | None] = relationship(back_populates="operation_logs")


class ProductPlaceholderCleanupLog(Base):
    __tablename__ = "product_placeholder_cleanup_logs"
    __table_args__ = (
        Index("ix_product_placeholder_cleanup_old", "old_product_id", "created_at"),
        Index("ix_product_placeholder_cleanup_new", "new_product_id", "created_at"),
        Index("ix_product_placeholder_cleanup_jan", "jan"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    old_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    new_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    jan: Mapped[str] = mapped_column(String(32), nullable=False)
    migrated_association_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class StoreBrand(Base):
    __tablename__ = "store_brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name_cn: Mapped[str | None] = mapped_column(String(128))
    name_ja: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    stores: Mapped[list[Store]] = relationship(back_populates="brand")

    @property
    def display_name(self) -> str:
        return f"{self.name_cn or '中文名待补'}｜{self.name_ja or '日文名待补'}"


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (
        Index("uq_stores_receipt_code_not_null", "receipt_store_code", unique=True, sqlite_where=text("receipt_store_code IS NOT NULL")),
        Index("uq_stores_phone_not_null", "normalized_phone", unique=True, sqlite_where=text("normalized_phone IS NOT NULL")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand_id: Mapped[int | None] = mapped_column(ForeignKey("store_brands.id", ondelete="SET NULL"), index=True)
    name_cn: Mapped[str | None] = mapped_column(String(128))
    name_ja: Mapped[str | None] = mapped_column(String(128))
    raw_name: Mapped[str | None] = mapped_column(String(255))
    phone: Mapped[str | None] = mapped_column(String(50))
    normalized_phone: Mapped[str | None] = mapped_column(String(32))
    postal_code: Mapped[str | None] = mapped_column(String(20))
    normalized_postal_code: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(Text)
    normalized_address: Mapped[str | None] = mapped_column(Text)
    receipt_store_code: Mapped[str | None] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    brand: Mapped[StoreBrand | None] = relationship(back_populates="stores")
    aliases: Mapped[list[StoreAlias]] = relationship(back_populates="store", cascade="all, delete-orphan")
    receipts: Mapped[list[Receipt]] = relationship(back_populates="store")
    purchase_batches: Mapped[list[PurchaseBatch]] = relationship(back_populates="store")
    restock_lists: Mapped[list[RestockList]] = relationship(back_populates="store")
    field_purchase_batches: Mapped[list[FieldPurchaseBatch]] = relationship(back_populates="store")

    @property
    def display_name(self) -> str:
        # Never show a "中文名待补"/"日文名待补" placeholder: fall back to
        # whichever side actually has a name instead of fabricating one.
        cn = self.name_cn or (self.name if not self.name_ja else None)
        ja = self.name_ja
        if cn and ja:
            return f"{cn}｜{ja}"
        return cn or ja or self.name


class StoreAlias(Base):
    __tablename__ = "store_aliases"
    __table_args__ = (UniqueConstraint("normalized_alias", name="uq_store_aliases_normalized"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), nullable=False, index=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    source_receipt_id: Mapped[int | None] = mapped_column(ForeignKey("receipts.id", ondelete="SET NULL"), index=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    store: Mapped[Store] = relationship(back_populates="aliases")
    source_receipt: Mapped[Receipt | None] = relationship()


class Location(Base):
    __tablename__ = "locations"
    __table_args__ = (
        CheckConstraint(
            "location_type IN ('qinsi_warehouse','local_physical','transit','system_status')",
            name="ck_locations_type",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    internal_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    location_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    is_qinsi_warehouse: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    qinsi_inventory_lines: Mapped[list[QinsiInventorySnapshotLine]] = relationship(back_populates="warehouse")


class PurchaseBatch(Base):
    __tablename__ = "purchase_batches"
    __table_args__ = (
        CheckConstraint(
            "status IN ('confirmed','pending_qinsi_submission','cancelled')",
            name="ck_purchase_batches_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="RESTRICT"), unique=True, nullable=False, index=True)
    gpt_batch_id: Mapped[int] = mapped_column(ForeignKey("receipt_batches.id", ondelete="RESTRICT"), nullable=False, index=True)
    purchased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    store_name: Mapped[str | None] = mapped_column(String(255))
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), index=True)
    operator_name: Mapped[str | None] = mapped_column(String(128), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="confirmed", nullable=False, index=True)
    default_initial_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    default_qinsi_warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    receipt: Mapped[Receipt] = relationship(back_populates="purchase_batch")
    store: Mapped[Store | None] = relationship(back_populates="purchase_batches")
    gpt_batch: Mapped[ReceiptBatch] = relationship()
    default_initial_location: Mapped[Location] = relationship(foreign_keys=[default_initial_location_id])
    default_qinsi_warehouse: Mapped[Location | None] = relationship(foreign_keys=[default_qinsi_warehouse_id])
    items: Mapped[list[PurchaseBatchItem]] = relationship(back_populates="purchase_batch", cascade="all, delete-orphan", order_by="PurchaseBatchItem.id")
    qinsi_export_jobs: Mapped[list[QinsiPurchaseExportJob]] = relationship(back_populates="purchase_batch")


class PurchaseBatchItem(Base):
    __tablename__ = "purchase_batch_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_purchase_batch_items_quantity_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_batch_id: Mapped[int] = mapped_column(ForeignKey("purchase_batches.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("receipt_items.id", ondelete="RESTRICT"), unique=True, nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[int | None] = mapped_column(Integer)
    discount_amount: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    actual_line_amount: Mapped[int | None] = mapped_column(Integer)
    initial_location_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    qinsi_target_warehouse_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    target_warehouse_overridden: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    purchase_batch: Mapped[PurchaseBatch] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="purchase_details")
    receipt_item: Mapped[ReceiptItem] = relationship(back_populates="purchase_detail")
    initial_location: Mapped[Location] = relationship(foreign_keys=[initial_location_id])
    qinsi_target_warehouse: Mapped[Location] = relationship(foreign_keys=[qinsi_target_warehouse_id])
    qinsi_export_lines: Mapped[list[QinsiPurchaseExportLine]] = relationship(back_populates="purchase_batch_item")
    restock_list_items: Mapped[list[RestockListItem]] = relationship(back_populates="purchase_batch_item")


class RestockList(Base):
    __tablename__ = "restock_lists"
    __table_args__ = (
        CheckConstraint("status IN ('draft','active','completed','cancelled')", name="ck_restock_lists_status"),
        CheckConstraint(
            "source_type IN ('manual','store_history','watched_products','purchase_analysis')",
            name="ck_restock_lists_source_type",
        ),
        Index("ix_restock_lists_store_status", "store_id", "status"),
        Index("ix_restock_lists_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    store: Mapped[Store] = relationship(back_populates="restock_lists")
    items: Mapped[list[RestockListItem]] = relationship(
        back_populates="restock_list", cascade="all, delete-orphan", order_by="RestockListItem.sort_value",
    )


class RestockListItem(Base):
    __tablename__ = "restock_list_items"
    __table_args__ = (
        UniqueConstraint("restock_list_id", "product_id", name="uq_restock_list_items_list_product"),
        CheckConstraint(
            "status IN ('to_check','found','not_found','purchased','skipped')",
            name="ck_restock_list_items_status",
        ),
        CheckConstraint("planned_quantity IS NULL OR planned_quantity > 0", name="ck_restock_items_planned_quantity"),
        CheckConstraint("actual_purchase_quantity IS NULL OR actual_purchase_quantity > 0", name="ck_restock_items_actual_quantity"),
        CheckConstraint("actual_purchase_price IS NULL OR actual_purchase_price > 0", name="ck_restock_items_actual_price"),
        Index("ix_restock_list_items_product_status", "product_id", "status"),
        Index("ix_restock_list_items_list_sort", "restock_list_id", "sort_value"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    restock_list_id: Mapped[int] = mapped_column(ForeignKey("restock_lists.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    added_source: Mapped[str] = mapped_column(String(30), default="manual", nullable=False)
    sort_value: Mapped[int] = mapped_column(Integer, default=1000, nullable=False)
    planned_quantity: Mapped[int | None] = mapped_column(Integer)
    target_purchase_price_snapshot: Mapped[int | None] = mapped_column(Integer)
    latest_purchase_price_snapshot: Mapped[int | None] = mapped_column(Integer)
    historical_lowest_purchase_price_snapshot: Mapped[int | None] = mapped_column(Integer)
    latest_store_purchase_price_snapshot: Mapped[int | None] = mapped_column(Integer)
    store_lowest_purchase_price_snapshot: Mapped[int | None] = mapped_column(Integer)
    latest_store_purchase_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qinsi_quantity_snapshot: Mapped[int | None] = mapped_column(Integer)
    qinsi_snapshot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    online_lowest_price_snapshot: Mapped[int | None] = mapped_column(Integer)
    online_price_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recommendation_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="to_check", nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    actual_purchase_quantity: Mapped[int | None] = mapped_column(Integer)
    actual_purchase_price: Mapped[int | None] = mapped_column(Integer)
    watch_config_id: Mapped[int | None] = mapped_column(ForeignKey("product_watch_configs.id", ondelete="SET NULL"), index=True)
    online_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("product_watch_snapshots.id", ondelete="SET NULL"), index=True)
    qinsi_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("qinsi_inventory_snapshots.id", ondelete="SET NULL"), index=True)
    purchase_batch_item_id: Mapped[int | None] = mapped_column(ForeignKey("purchase_batch_items.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    restock_list: Mapped[RestockList] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(back_populates="restock_list_items")
    watch_config: Mapped[ProductWatchConfig | None] = relationship()
    online_snapshot: Mapped[ProductWatchSnapshot | None] = relationship()
    qinsi_snapshot: Mapped[QinsiInventorySnapshot | None] = relationship()
    purchase_batch_item: Mapped[PurchaseBatchItem | None] = relationship(back_populates="restock_list_items")


class QinsiPurchaseExportJob(Base):
    __tablename__ = "qinsi_purchase_export_jobs"
    __table_args__ = (
        CheckConstraint("export_type IN ('new_product','restock')", name="ck_qinsi_purchase_export_jobs_type"),
        CheckConstraint(
            "status IN ('generated','imported','partially_failed','failed','cancelled')",
            name="ck_qinsi_purchase_export_jobs_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    export_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    selection_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    export_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    purchase_batch_id: Mapped[int] = mapped_column(ForeignKey("purchase_batches.id", ondelete="RESTRICT"), nullable=False, index=True)
    selected_batch_ids_json: Mapped[str | None] = mapped_column(Text)
    qinsi_target_warehouse_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    parent_export_job_id: Mapped[int | None] = mapped_column(ForeignKey("qinsi_purchase_export_jobs.id", ondelete="RESTRICT"), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="generated", nullable=False, index=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    confirmation_note: Mapped[str | None] = mapped_column(Text)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    purchase_batch: Mapped[PurchaseBatch] = relationship(back_populates="qinsi_export_jobs")
    qinsi_target_warehouse: Mapped[Location] = relationship()
    parent_export_job: Mapped[QinsiPurchaseExportJob | None] = relationship(remote_side=[id])
    lines: Mapped[list[QinsiPurchaseExportLine]] = relationship(
        back_populates="export_job", cascade="all, delete-orphan", order_by="QinsiPurchaseExportLine.row_no",
    )

    @property
    def selected_batch_ids(self) -> list[int]:
        try:
            values = json.loads(self.selected_batch_ids_json or "[]")
        except (TypeError, ValueError):
            values = []
        batch_ids = [int(value) for value in values if str(value).isdigit()]
        return batch_ids or [self.purchase_batch_id]


class QinsiPurchaseExportLine(Base):
    __tablename__ = "qinsi_purchase_export_lines"
    __table_args__ = (
        CheckConstraint("status IN ('generated','imported','failed','cancelled')", name="ck_qinsi_purchase_export_lines_status"),
        CheckConstraint("quantity > 0", name="ck_qinsi_purchase_export_lines_quantity_positive"),
        UniqueConstraint("export_job_id", "purchase_batch_item_id", name="uq_qinsi_purchase_export_line_job_item"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    export_job_id: Mapped[int] = mapped_column(ForeignKey("qinsi_purchase_export_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    purchase_batch_id: Mapped[int] = mapped_column(ForeignKey("purchase_batches.id", ondelete="RESTRICT"), nullable=False, index=True)
    purchase_batch_item_id: Mapped[int] = mapped_column(ForeignKey("purchase_batch_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    receipt_id: Mapped[int] = mapped_column(ForeignKey("receipts.id", ondelete="RESTRICT"), nullable=False, index=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("receipt_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    qinsi_target_warehouse_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    internal_sku: Mapped[str] = mapped_column(String(32), nullable=False)
    jan: Mapped[str | None] = mapped_column(String(32))
    qinsi_product_code: Mapped[str] = mapped_column(String(100), nullable=False)
    product_name: Mapped[str] = mapped_column(String(255), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_price: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="generated", nullable=False, index=True)
    failure_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    export_job: Mapped[QinsiPurchaseExportJob] = relationship(back_populates="lines")
    purchase_batch: Mapped[PurchaseBatch] = relationship()
    purchase_batch_item: Mapped[PurchaseBatchItem] = relationship(back_populates="qinsi_export_lines")
    receipt: Mapped[Receipt] = relationship()
    receipt_item: Mapped[ReceiptItem] = relationship()
    product: Mapped[Product] = relationship()
    qinsi_target_warehouse: Mapped[Location] = relationship()
    source: Mapped[QinsiPurchaseExportLineSource] = relationship(
        back_populates="export_line", cascade="all, delete-orphan", uselist=False,
    )


class QinsiPurchaseExportLineSource(Base):
    __tablename__ = "qinsi_purchase_export_line_sources"
    __table_args__ = (
        UniqueConstraint("export_line_id", name="uq_qinsi_purchase_export_line_source_line"),
        Index(
            "uq_qinsi_active_purchase_batch_item",
            "purchase_batch_item_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    export_line_id: Mapped[int] = mapped_column(ForeignKey("qinsi_purchase_export_lines.id", ondelete="CASCADE"), nullable=False, index=True)
    purchase_batch_item_id: Mapped[int] = mapped_column(ForeignKey("purchase_batch_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    export_line: Mapped[QinsiPurchaseExportLine] = relationship(back_populates="source")


class QinsiInventorySnapshot(Base):
    __tablename__ = "qinsi_inventory_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed','completed_with_issues','failed')",
            name="ck_qinsi_inventory_snapshots_status",
        ),
        Index("uq_qinsi_inventory_snapshots_file_hash", "file_hash", unique=True),
        Index("ix_qinsi_inventory_snapshots_data_time", "data_at", "imported_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    data_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_system: Mapped[str] = mapped_column(String(30), default="qinsi", nullable=False)
    snapshot_type: Mapped[str] = mapped_column(String(30), default="counted_inventory", nullable=False)
    source_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("qinsi_import_batches.id", ondelete="SET NULL"), unique=True,
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unmatched_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    exception_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    lines: Mapped[list[QinsiInventorySnapshotLine]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", order_by="QinsiInventorySnapshotLine.id",
    )


class QinsiInventorySnapshotLine(Base):
    __tablename__ = "qinsi_inventory_snapshot_lines"
    __table_args__ = (
        CheckConstraint(
            "matching_status IN ('matched','unmatched','conflict','ignored')",
            name="ck_qinsi_inventory_snapshot_lines_matching_status",
        ),
        Index("ix_qinsi_inventory_snapshot_lines_product_snapshot", "product_id", "snapshot_id"),
        Index("ix_qinsi_inventory_snapshot_lines_warehouse_snapshot", "warehouse_id", "snapshot_id"),
        Index("ix_qinsi_inventory_snapshot_lines_status", "snapshot_id", "matching_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("qinsi_inventory_snapshots.id", ondelete="CASCADE"), nullable=False)
    original_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_product_name: Mapped[str | None] = mapped_column(String(255))
    jan: Mapped[str | None] = mapped_column(String(32))
    qinsi_product_code: Mapped[str | None] = mapped_column(String(100))
    internal_sku: Mapped[str | None] = mapped_column(String(32))
    raw_warehouse_name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[int | None] = mapped_column(Integer)
    current_quantity: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    raw_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id", ondelete="SET NULL"))
    matching_method: Mapped[str | None] = mapped_column(String(40))
    matching_status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    warehouse_status: Mapped[str] = mapped_column(String(20), default="matched", nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    snapshot: Mapped[QinsiInventorySnapshot] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship(back_populates="qinsi_inventory_lines")
    warehouse: Mapped[Location | None] = relationship(back_populates="qinsi_inventory_lines")


class QinsiSalesSummarySnapshot(Base):
    """秦丝报表 -> 进销存汇总 export, for a single user-declared date range.

    Deliberately NOT the inventory authority (QinsiInventorySnapshot stays
    that) -- this only carries sales/purchase FACTS as QinSi reported them
    for one period. One table serves any period length (1/7/30/自定义 days);
    period_days is derived from the user-entered dates, never guessed from a
    filename."""
    __tablename__ = "qinsi_sales_summary_snapshots"
    __table_args__ = (
        CheckConstraint(
            "status IN ('completed','completed_with_issues','failed')",
            name="ck_qinsi_sales_summary_snapshots_status",
        ),
        CheckConstraint("period_end >= period_start", name="ck_qinsi_sales_summary_snapshots_period_order"),
        Index("uq_qinsi_sales_summary_snapshots_file_hash", "file_hash", unique=True),
        Index("ix_qinsi_sales_summary_snapshots_period", "period_start", "period_end"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_no: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    period_days: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    matched_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unmatched_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    lines: Mapped[list[QinsiSalesSummaryLine]] = relationship(
        back_populates="snapshot", cascade="all, delete-orphan", order_by="QinsiSalesSummaryLine.id",
    )


class QinsiSalesSummaryLine(Base):
    __tablename__ = "qinsi_sales_summary_lines"
    __table_args__ = (
        CheckConstraint(
            "match_status IN ('matched','unmatched','conflict')",
            name="ck_qinsi_sales_summary_lines_match_status",
        ),
        Index("ix_qinsi_sales_summary_lines_product_snapshot", "product_id", "snapshot_id"),
        Index("ix_qinsi_sales_summary_lines_status", "snapshot_id", "match_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    snapshot_id: Mapped[int] = mapped_column(
        ForeignKey("qinsi_sales_summary_snapshots.id", ondelete="CASCADE"), nullable=False,
    )
    original_row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    product_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    qinsi_product_code: Mapped[str | None] = mapped_column(String(100))
    jan_candidate: Mapped[str | None] = mapped_column(String(32))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    match_status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    matching_method: Mapped[str | None] = mapped_column(String(40))
    purchase_quantity: Mapped[int | None] = mapped_column(Integer)
    purchase_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    sales_quantity: Mapped[int | None] = mapped_column(Integer)
    sales_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2))
    customer_count: Mapped[int | None] = mapped_column(Integer)
    reported_current_inventory: Mapped[int | None] = mapped_column(Integer)
    reported_support_sales_days: Mapped[int | None] = mapped_column(Integer)
    raw_row_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    snapshot: Mapped[QinsiSalesSummarySnapshot] = relationship(back_populates="lines")
    product: Mapped[Product | None] = relationship(back_populates="qinsi_sales_summary_lines")


class QinsiProductMapping(Base):
    __tablename__ = "qinsi_product_mappings"
    __table_args__ = (
        Index("uq_qinsi_product_mappings_code", "qinsi_product_code", unique=True),
        Index("ix_qinsi_product_mappings_product", "product_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    qinsi_product_code: Mapped[str] = mapped_column(String(100), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    source_snapshot_line_id: Mapped[int | None] = mapped_column(
        ForeignKey("qinsi_inventory_snapshot_lines.id", ondelete="SET NULL")
    )
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    product: Mapped[Product] = relationship(back_populates="qinsi_product_mappings")
    source_snapshot_line: Mapped[QinsiInventorySnapshotLine | None] = relationship()


class QinsiConflictResolution(Base):
    __tablename__ = "qinsi_conflict_resolutions"
    __table_args__ = (
        Index("uq_qinsi_conflict_resolutions_key", "conflict_key", unique=True),
        Index("ix_qinsi_conflict_resolutions_barcode", "barcode"),
        Index("ix_qinsi_conflict_resolutions_auto", "auto_apply"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    conflict_key: Mapped[str] = mapped_column(String(255), nullable=False)
    barcode: Mapped[str | None] = mapped_column(String(100))
    qinsi_product_codes: Mapped[str] = mapped_column(Text, nullable=False)
    resolution_type: Mapped[str] = mapped_column(String(40), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    auto_apply: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    receipt_item_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_items.id", ondelete="RESTRICT"))
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class QinsiImportBatch(Base):
    __tablename__ = "qinsi_import_batches"
    __table_args__ = (
        Index("uq_qinsi_import_batches_file_hash", "file_hash", unique=True),
        Index("ix_qinsi_import_batches_business_batch", "business_batch_key"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    business_batch_key: Mapped[str | None] = mapped_column(String(100))
    source_system: Mapped[str] = mapped_column(String(30), default="qinsi", nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    file_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    parse_version: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    new_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    update_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unchanged_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    rows: Mapped[list[QinsiGoodsImportRow]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="QinsiGoodsImportRow.excel_row_number",
    )


class QinsiGoodsImportRow(Base):
    __tablename__ = "qinsi_goods_import_rows"
    __table_args__ = (
        UniqueConstraint("import_batch_id", "excel_row_number", name="uq_qinsi_goods_rows_batch_row"),
        Index("ix_qinsi_goods_rows_code", "qinsi_product_code"),
        Index("ix_qinsi_goods_rows_barcode", "barcode"),
        Index("ix_qinsi_goods_rows_status", "import_batch_id", "validation_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("qinsi_import_batches.id", ondelete="CASCADE"), nullable=False,
    )
    source_file_name: Mapped[str] = mapped_column(String(255), nullable=False)
    sheet_name: Mapped[str] = mapped_column(String(100), default="商品导入", nullable=False)
    excel_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    qinsi_product_code: Mapped[str | None] = mapped_column(String(100))
    barcode: Mapped[str | None] = mapped_column(String(100))
    parsed_data: Mapped[str] = mapped_column(Text, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(30), nullable=False)
    warnings: Mapped[str | None] = mapped_column(Text)
    errors: Mapped[str | None] = mapped_column(Text)
    conflict_json: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    batch: Mapped[QinsiImportBatch] = relationship(back_populates="rows")
    product: Mapped[Product | None] = relationship()

    @property
    def row_no(self) -> int:
        return self.excel_row_number

    @property
    def status(self) -> str:
        return self.validation_status

    @property
    def parsed_json(self) -> str:
        return self.parsed_data

    @property
    def warnings_json(self) -> str | None:
        return self.warnings

    @property
    def error_message(self) -> str | None:
        if self.errors:
            try:
                return "；".join(json.loads(self.errors))
            except (TypeError, ValueError):
                return self.errors
        if self.conflict_json:
            try:
                return "；".join(item.get("message", "") for item in json.loads(self.conflict_json) if item.get("message"))
            except (TypeError, ValueError):
                return self.conflict_json
        return None


class QinsiMasterValue(Base):
    __tablename__ = "qinsi_master_values"
    __table_args__ = (
        UniqueConstraint("source_system", "master_type", "source_name", name="uq_qinsi_master_source_name"),
        Index("ix_qinsi_master_type_active", "master_type", "is_active"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    master_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_name: Mapped[str] = mapped_column(String(255), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_system: Mapped[str] = mapped_column(String(30), default="qinsi", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("qinsi_import_batches.id", ondelete="SET NULL"),
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class ProductBarcode(Base):
    __tablename__ = "product_barcodes"
    __table_args__ = (
        Index("ix_product_barcodes_barcode", "barcode"),
        UniqueConstraint("product_id", "barcode", name="uq_product_barcode_product_value"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    barcode: Mapped[str] = mapped_column(String(100), nullable=False)
    source_system: Mapped[str] = mapped_column(String(30), default="qinsi", nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    product: Mapped[Product] = relationship(back_populates="barcodes")


class FieldPurchaseBatch(Base):
    __tablename__ = "field_purchase_batches"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','COMPLETED','CANCELLED')", name="ck_field_purchase_batches_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    client_request_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="RESTRICT"), index=True)
    operator_name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    store: Mapped[Store | None] = relationship(back_populates="field_purchase_batches")
    items: Mapped[list[FieldPurchaseItem]] = relationship(
        back_populates="batch", cascade="all, delete-orphan", order_by="FieldPurchaseItem.id",
    )


class FieldPurchaseItem(Base):
    __tablename__ = "field_purchase_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_field_purchase_items_quantity_positive"),
        CheckConstraint(
            "status IN ('LOCAL_DRAFT','UPLOAD_PENDING','ENRICHMENT_PENDING','ENRICHING',"
            "'NEEDS_REVIEW','READY','FAILED_RETRYABLE','FAILED_MANUAL','CONFIRMED')",
            name="ck_field_purchase_items_status",
        ),
        CheckConstraint(
            "jan IS NOT NULL OR temporary_id IS NOT NULL",
            name="ck_field_purchase_items_identity",
        ),
        Index(
            "uq_field_purchase_items_batch_product",
            "batch_id", "product_id", unique=True, sqlite_where=text("product_id IS NOT NULL"),
        ),
        Index(
            "uq_field_purchase_items_batch_unmatched_jan",
            "batch_id", "jan", unique=True,
            sqlite_where=text("product_id IS NULL AND jan IS NOT NULL"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("field_purchase_batches.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), index=True)
    enrichment_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_enrichment_tasks.id", ondelete="SET NULL"), index=True,
    )
    jan: Mapped[str | None] = mapped_column(String(32), index=True)
    temporary_id: Mapped[str | None] = mapped_column(String(80), unique=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="LOCAL_DRAFT", nullable=False, index=True)
    name_cn: Mapped[str | None] = mapped_column(String(128))
    name_ja: Mapped[str | None] = mapped_column(String(128))
    brand: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    unit_name: Mapped[str | None] = mapped_column(String(128))
    unit_price: Mapped[int | None] = mapped_column(Integer)
    product_image_path: Mapped[str | None] = mapped_column(Text)
    captured_by: Mapped[str] = mapped_column(String(128), nullable=False)
    first_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_scanned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    batch: Mapped[FieldPurchaseBatch] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship(back_populates="field_purchase_items")
    enrichment_task: Mapped[ProductEnrichmentTask | None] = relationship()
    tag_evidence: Mapped[list[TagEvidence]] = relationship(
        back_populates="item", cascade="all, delete-orphan", order_by="TagEvidence.id",
    )
    sync_requests: Mapped[list[FieldPurchaseSyncRequest]] = relationship(back_populates="item")
    audit_logs: Mapped[list[EnrichmentAuditLog]] = relationship(back_populates="field_purchase_item")


class TagEvidence(Base):
    __tablename__ = "tag_evidence"
    __table_args__ = (
        UniqueConstraint("field_purchase_item_id", "sha256", name="uq_tag_evidence_item_hash"),
        CheckConstraint(
            "ocr_status IN ('PENDING','UNCONFIGURED','PROCESSING','COMPLETED','FAILED_RETRYABLE','FAILED_MANUAL')",
            name="ck_tag_evidence_ocr_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    field_purchase_item_id: Mapped[int] = mapped_column(
        ForeignKey("field_purchase_items.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    ocr_status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False, index=True)
    ocr_text: Mapped[str | None] = mapped_column(Text)
    ocr_confidence: Mapped[float | None] = mapped_column(Float)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    item: Mapped[FieldPurchaseItem] = relationship(back_populates="tag_evidence")


class ProductSerial(Base):
    __tablename__ = "product_serials"
    __table_args__ = (
        CheckConstraint("status IN ('ACTIVE','USED','VOID')", name="ck_product_serials_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    serial_value: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="manual", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    product: Mapped[Product] = relationship(back_populates="serials")


class FieldPurchaseSyncRequest(Base):
    __tablename__ = "field_purchase_sync_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    client_request_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    request_type: Mapped[str] = mapped_column(String(40), nullable=False)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("field_purchase_batches.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    item_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_purchase_items.id", ondelete="SET NULL"), index=True,
    )
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    item: Mapped[FieldPurchaseItem | None] = relationship(back_populates="sync_requests")


class DurableBackgroundJob(Base):
    __tablename__ = "durable_background_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('PENDING','RUNNING','COMPLETED','FAILED_RETRYABLE','FAILED_MANUAL')",
            name="ck_durable_background_jobs_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlatformProviderState(Base):
    __tablename__ = "platform_provider_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)
    configured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    credentials_valid: Mapped[bool | None] = mapped_column(Boolean)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_test_status: Mapped[str | None] = mapped_column(String(30))
    last_http_status: Mapped[int | None] = mapped_column(Integer)
    last_result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    recent_error: Mapped[str | None] = mapped_column(Text)
    request_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PlatformLookupResult(Base):
    __tablename__ = "platform_lookup_results"
    __table_args__ = (
        CheckConstraint("link_type IN ('product','search')", name="ck_platform_lookup_results_link_type"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    price_search_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("price_search_runs.id", ondelete="CASCADE"), index=True,
    )
    field_purchase_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_purchase_items.id", ondelete="SET NULL"), index=True,
    )
    platform: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    jan: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    price: Mapped[int | None] = mapped_column(Integer)
    shipping_fee: Mapped[int | None] = mapped_column(Integer)
    total_price: Mapped[int | None] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="JPY", nullable=False)
    availability: Mapped[str | None] = mapped_column(String(30))
    seller: Mapped[str | None] = mapped_column(String(255))
    product_url: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    link_type: Mapped[str] = mapped_column(String(20), default="product", nullable=False)
    jan_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    match_type: Mapped[str] = mapped_column(String(30), default="UNVERIFIED", nullable=False)
    confidence: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(60))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    search_run: Mapped[PriceSearchRun | None] = relationship(back_populates="platform_results")


class EnrichmentAuditLog(Base):
    __tablename__ = "enrichment_audit_logs"
    __table_args__ = (
        CheckConstraint(
            "action IN ('SELECT_CANDIDATE','REJECT_CANDIDATE','MANUAL_EDIT','BULK_EDIT',"
            "'RETRY','CONFIRM','BIND_EXISTING')",
            name="ck_enrichment_audit_logs_action",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    field_purchase_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("field_purchase_items.id", ondelete="SET NULL"), index=True,
    )
    enrichment_task_id: Mapped[int | None] = mapped_column(
        ForeignKey("product_enrichment_tasks.id", ondelete="SET NULL"), index=True,
    )
    action: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text)
    after_json: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(50), default="web", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    field_purchase_item: Mapped[FieldPurchaseItem | None] = relationship(back_populates="audit_logs")


class ImportJob(Base):
    __tablename__ = "import_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ImportRow(Base):
    __tablename__ = "import_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_job_id: Mapped[int] = mapped_column(ForeignKey("import_jobs.id", ondelete="CASCADE"), nullable=False)
    row_no: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    parsed_json: Mapped[str | None] = mapped_column(Text)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))


class ProductMatchLog(Base):
    __tablename__ = "product_match_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("receipt_items.id", ondelete="CASCADE"), nullable=False, index=True)
    old_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    new_product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"))
    method: Mapped[str] = mapped_column(String(50), nullable=False)
    decision: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ProductEnrichmentTask(Base):
    __tablename__ = "product_enrichment_tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','running','completed','completed_with_warnings','needs_review','failed')",
            name="ck_product_enrichment_tasks_status",
        ),
        Index("ix_product_enrichment_tasks_jan_status", "jan", "status"),
        Index("ix_product_enrichment_tasks_trigger_source", "trigger_source"),
        Index(
            "uq_product_enrichment_tasks_jan_open_or_success",
            "jan",
            unique=True,
            sqlite_where=text("status IN ('pending','running','completed','completed_with_warnings','needs_review')"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    jan: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    trigger_source: Mapped[str] = mapped_column(String(50), nullable=False)
    provider_codes_json: Mapped[str | None] = mapped_column(Text)
    selected_data_json: Mapped[str | None] = mapped_column(Text)
    deepseek_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    deepseek_name_key: Mapped[str | None] = mapped_column(String(64), index=True)
    image_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    product: Mapped[Product | None] = relationship()
    candidates: Mapped[list[ProductEnrichmentCandidate]] = relationship(back_populates="task", cascade="all, delete-orphan", order_by="ProductEnrichmentCandidate.score.desc()")
    sources: Mapped[list[ProductEnrichmentSource]] = relationship(back_populates="task", cascade="all, delete-orphan")


class ProductEnrichmentCandidate(Base):
    __tablename__ = "product_enrichment_candidates"
    __table_args__ = (UniqueConstraint("task_id", "platform", "source_url", name="uq_product_enrichment_candidate_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("product_enrichment_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    jan: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name_ja: Mapped[str | None] = mapped_column(Text)
    brand: Mapped[str | None] = mapped_column(String(128))
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    specification: Mapped[str | None] = mapped_column(String(255))
    capacity: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(64))
    model_number: Mapped[str | None] = mapped_column(String(128))
    package_count: Mapped[str | None] = mapped_column(String(64))
    net_weight_g: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    volume_ml: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    length_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    width_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    height_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    depth_mm: Mapped[Decimal | None] = mapped_column(Numeric(18, 3))
    pack_quantity: Mapped[int | None] = mapped_column(Integer)
    spec_text: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    item_price: Mapped[int | None] = mapped_column(Integer)
    shipping_price: Mapped[int | None] = mapped_column(Integer)
    total_price: Mapped[int | None] = mapped_column(Integer)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    score: Mapped[float] = mapped_column(Float, default=0, nullable=False)
    warnings_json: Mapped[str | None] = mapped_column(Text)
    provider_summary_json: Mapped[str | None] = mapped_column(Text)
    selected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    task: Mapped[ProductEnrichmentTask] = relationship(back_populates="candidates")


class ProductEnrichmentSource(Base):
    __tablename__ = "product_enrichment_sources"
    __table_args__ = (UniqueConstraint("task_id", "source_type", "source_id", name="uq_product_enrichment_task_source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("product_enrichment_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)
    source_id: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    task: Mapped[ProductEnrichmentTask] = relationship(back_populates="sources")


class ProductTranslationCache(Base):
    __tablename__ = "product_translation_cache"
    __table_args__ = (UniqueConstraint("jan", "name_ja_hash", name="uq_product_translation_jan_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    jan: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name_ja_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    name_ja: Mapped[str] = mapped_column(Text, nullable=False)
    response_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class QinsiExportJob(Base):
    __tablename__ = "qinsi_export_jobs"
    __table_args__ = (CheckConstraint("status IN ('pending','exported','confirmed','failed')", name="ck_qinsi_export_jobs_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    export_filename: Mapped[str | None] = mapped_column(String(255))
    file_content: Mapped[bytes | None] = mapped_column(LargeBinary)
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(128))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(128))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class QinsiExportLine(Base):
    __tablename__ = "qinsi_export_lines"
    __table_args__ = (
        CheckConstraint("status IN ('pending','exported','confirmed','failed')", name="ck_qinsi_export_lines_status"),
        CheckConstraint("quantity > 0", name="ck_qinsi_export_lines_quantity_positive"),
        UniqueConstraint("job_id", "product_id", name="uq_qinsi_export_line_job_product"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("qinsi_export_jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"), nullable=False, index=True)
    qinsi_product_code: Mapped[str | None] = mapped_column(String(100))
    product_name: Mapped[str | None] = mapped_column(String(255))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    purchase_price: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class QinsiExportLineSource(Base):
    __tablename__ = "qinsi_export_line_sources"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_qinsi_export_line_sources_quantity_positive"),
        Index("uq_qinsi_active_receipt_item", "receipt_item_id", unique=True, sqlite_where=text("is_active = 1")),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    export_line_id: Mapped[int] = mapped_column(ForeignKey("qinsi_export_lines.id", ondelete="CASCADE"), nullable=False, index=True)
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("receipt_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Marketplace(Base):
    __tablename__ = "marketplaces"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class PriceSearchRun(Base):
    __tablename__ = "price_search_runs"
    __table_args__ = (CheckConstraint("status IN ('pending','running','completed','failed')", name="ck_price_search_runs_status"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    jan: Mapped[str | None] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    is_new_candidate: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    cache_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    provider_summary_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    product: Mapped[Product | None] = relationship(back_populates="price_search_runs")
    offers: Mapped[list[ProductOffer]] = relationship(back_populates="search_run", cascade="all, delete-orphan", order_by="ProductOffer.total_price")
    provider_attempts: Mapped[list[PriceProviderAttempt]] = relationship(back_populates="search_run", cascade="all, delete-orphan", order_by="PriceProviderAttempt.id")
    lookup_histories: Mapped[list[PriceLookupHistory]] = relationship(back_populates="search_run", cascade="all, delete-orphan")
    platform_results: Mapped[list[PlatformLookupResult]] = relationship(
        back_populates="search_run", cascade="all, delete-orphan", order_by="PlatformLookupResult.id",
    )


class ProductOffer(Base):
    __tablename__ = "product_offers"
    __table_args__ = (
        CheckConstraint("item_price >= 0", name="ck_product_offers_item_price"),
        CheckConstraint("shipping_price >= 0", name="ck_product_offers_shipping_price"),
        CheckConstraint("total_price >= 0", name="ck_product_offers_total_price"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    search_run_id: Mapped[int] = mapped_column(ForeignKey("price_search_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    marketplace_id: Mapped[int] = mapped_column(ForeignKey("marketplaces.id", ondelete="RESTRICT"), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    jan: Mapped[str | None] = mapped_column(String(32), index=True)
    title: Mapped[str | None] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text)
    seller: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    item_price: Mapped[int] = mapped_column(Integer, nullable=False)
    shipping_price: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    shipping_known: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    total_price: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="JPY", nullable=False)
    stock_status: Mapped[str | None] = mapped_column(String(30))
    listing_type: Mapped[str] = mapped_column(String(20), default="single", nullable=False)
    condition: Mapped[str] = mapped_column(String(20), default="new", nullable=False)
    match_status: Mapped[str] = mapped_column(String(30), default="unreviewed", nullable=False)
    jan_match_status: Mapped[str] = mapped_column(String(30), default="unverified", nullable=False)
    spec_match_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False)
    is_subscription: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_trusted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_data_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    search_run: Mapped[PriceSearchRun] = relationship(back_populates="offers")
    marketplace: Mapped[Marketplace] = relationship()


class PriceProviderAttempt(Base):
    __tablename__ = "price_provider_attempts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('success','empty','timeout','error','unconfigured','manual_only')",
            name="ck_price_provider_attempts_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    search_run_id: Mapped[int] = mapped_column(ForeignKey("price_search_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    marketplace_id: Mapped[int | None] = mapped_column(ForeignKey("marketplaces.id", ondelete="SET NULL"), index=True)
    provider_code: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    search_run: Mapped[PriceSearchRun] = relationship(back_populates="provider_attempts")
    marketplace: Mapped[Marketplace | None] = relationship()


class PriceLookupHistory(Base):
    __tablename__ = "price_lookup_histories"
    __table_args__ = (CheckConstraint("current_store_price IS NULL OR current_store_price >= 0", name="ck_price_lookup_histories_store_price"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    search_run_id: Mapped[int] = mapped_column(ForeignKey("price_search_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    jan: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    current_store_price: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    lookup_source: Mapped[str] = mapped_column(String(20), default="manual", nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    search_run: Mapped[PriceSearchRun] = relationship(back_populates="lookup_histories")
    product: Mapped[Product | None] = relationship()


class ProductWatchConfig(Base):
    __tablename__ = "product_watch_configs"
    __table_args__ = (
        CheckConstraint("user_target_price IS NULL OR user_target_price > 0", name="ck_product_watch_user_price"),
        CheckConstraint("recommended_target_price IS NULL OR recommended_target_price > 0", name="ck_product_watch_recommended_price"),
        CheckConstraint("effective_target_price IS NULL OR effective_target_price > 0", name="ck_product_watch_effective_price"),
        CheckConstraint("frequency_tier IN ('low','normal','high','urgent')", name="ck_product_watch_frequency"),
        CheckConstraint(
            "source IN ('manual','purchase_recommendation','scan_recommendation','enrichment_recommendation')",
            name="ck_product_watch_source",
        ),
        Index("uq_product_watch_configs_product", "product_id", unique=True),
        Index("ix_product_watch_configs_enabled", "enabled"),
        Index("ix_product_watch_configs_due", "enabled", "next_check_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    user_target_price: Mapped[int | None] = mapped_column(Integer)
    recommended_target_price: Mapped[int | None] = mapped_column(Integer)
    effective_target_price: Mapped[int | None] = mapped_column(Integer)
    recommended_price_source: Mapped[str | None] = mapped_column(String(40))
    recommended_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monitor_restock: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    frequency_tier: Mapped[str] = mapped_column(String(20), default="normal", nullable=False)
    source: Mapped[str] = mapped_column(String(40), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    last_target_reached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pause_reason: Mapped[str | None] = mapped_column(String(100))
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_lowest_price: Mapped[int | None] = mapped_column(Integer)
    previous_lowest_price: Mapped[int | None] = mapped_column(Integer)
    historical_online_lowest_price: Mapped[int | None] = mapped_column(Integer)
    last_in_stock: Mapped[bool | None] = mapped_column(Boolean)
    last_provider_codes: Mapped[str | None] = mapped_column(String(255))
    last_check_status: Mapped[str | None] = mapped_column(String(30))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_summary: Mapped[str | None] = mapped_column(Text)
    failure_notification_sent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    product: Mapped[Product] = relationship(back_populates="watch_config")
    snapshots: Mapped[list[ProductWatchSnapshot]] = relationship(back_populates="watch_config")
    notifications: Mapped[list[ProductWatchNotification]] = relationship(back_populates="watch_config")


class ProductWatchRecommendation(Base):
    __tablename__ = "product_watch_recommendations"
    __table_args__ = (
        CheckConstraint(
            "reason IN ('history_purchase_count','cumulative_purchase_quantity','scan_count','restock_purchase','purchase_enrichment')",
            name="ck_product_watch_recommendation_reason",
        ),
        CheckConstraint("NOT (ignored = 1 AND accepted = 1)", name="ck_product_watch_recommendation_state"),
        Index("ix_product_watch_recommendations_product", "product_id"),
        Index("ix_product_watch_recommendations_pending", "accepted", "ignored", "recommended_at"),
        Index(
            "uq_product_watch_recommendations_pending_reason",
            "product_id",
            "reason",
            unique=True,
            sqlite_where=text("accepted = 0 AND ignored = 0"),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    recommended_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    ignored: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    accepted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    product: Mapped[Product] = relationship(back_populates="watch_recommendations")


class ProductWatchSnapshot(Base):
    __tablename__ = "product_watch_snapshots"
    __table_args__ = (
        CheckConstraint("status IN ('success','failed')", name="ck_product_watch_snapshots_status"),
        Index("ix_product_watch_snapshots_product_time", "product_id", "checked_at"),
        Index("ix_product_watch_snapshots_config_time", "watch_config_id", "checked_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_config_id: Mapped[int] = mapped_column(ForeignKey("product_watch_configs.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    price_lookup_history_id: Mapped[int | None] = mapped_column(ForeignKey("price_lookup_histories.id", ondelete="SET NULL"), index=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    lowest_item_price: Mapped[int | None] = mapped_column(Integer)
    shipping_price: Mapped[int | None] = mapped_column(Integer)
    total_price: Mapped[int | None] = mapped_column(Integer)
    marketplace: Mapped[str | None] = mapped_column(String(100))
    seller: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    is_in_stock: Mapped[bool | None] = mapped_column(Boolean)
    result_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    provider_codes: Mapped[str | None] = mapped_column(String(255))
    error_summary: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    watch_config: Mapped[ProductWatchConfig] = relationship(back_populates="snapshots")
    product: Mapped[Product] = relationship(back_populates="watch_snapshots")
    lookup_history: Mapped[PriceLookupHistory | None] = relationship()
    notifications: Mapped[list[ProductWatchNotification]] = relationship(back_populates="snapshot")


class ProductWatchNotification(Base):
    __tablename__ = "product_watch_notifications"
    __table_args__ = (
        CheckConstraint(
            "event_type IN ('target_reached','new_historical_low','restocked','monitor_failed')",
            name="ck_product_watch_notifications_type",
        ),
        Index("uq_product_watch_notifications_dedupe", "dedupe_key", unique=True),
        Index("ix_product_watch_notifications_unread", "is_read", "archived_at", "triggered_at"),
        Index("ix_product_watch_notifications_product", "product_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_config_id: Mapped[int] = mapped_column(ForeignKey("product_watch_configs.id", ondelete="CASCADE"), nullable=False)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("product_watch_snapshots.id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False)
    target_price: Mapped[int | None] = mapped_column(Integer)
    current_price: Mapped[int | None] = mapped_column(Integer)
    marketplace: Mapped[str | None] = mapped_column(String(100))
    seller: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    data_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    watch_config: Mapped[ProductWatchConfig] = relationship(back_populates="notifications")
    product: Mapped[Product] = relationship(back_populates="watch_notifications")
    snapshot: Mapped[ProductWatchSnapshot | None] = relationship(back_populates="notifications")


class MonitorSchedulerState(Base):
    __tablename__ = "monitor_scheduler_states"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, default="default")
    last_scan_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_scan_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error_summary: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PriceWatchRule(Base):
    __tablename__ = "price_watch_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True)
    marketplace_id: Mapped[int | None] = mapped_column(ForeignKey("marketplaces.id", ondelete="CASCADE"))
    target_total_price: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class PriceAlert(Base):
    __tablename__ = "price_alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("price_watch_rules.id", ondelete="CASCADE"), nullable=False, index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("product_offers.id", ondelete="CASCADE"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    triggered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50))
    wechat_name: Mapped[str | None] = mapped_column(String(128))
    # Deprecated single free-text address, kept only so pre-multi-address rows
    # stay readable; new code reads/writes CustomerAddress instead.
    address: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    sales_orders: Mapped[list[SalesOrder]] = relationship(back_populates="customer")
    addresses: Mapped[list[CustomerAddress]] = relationship(
        back_populates="customer", cascade="all, delete-orphan", order_by="CustomerAddress.id",
    )


class CustomerAddress(Base):
    __tablename__ = "customer_addresses"
    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"), nullable=False, index=True)
    recipient_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50))
    address: Mapped[str] = mapped_column(Text, nullable=False)
    label: Mapped[str | None] = mapped_column(String(50))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    customer: Mapped[Customer] = relationship(back_populates="addresses")


class Salesperson(Base):
    __tablename__ = "salespersons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    sales_orders: Mapped[list[SalesOrder]] = relationship(back_populates="salesperson")


class SalesOrder(Base):
    __tablename__ = "sales_orders"
    __table_args__ = (
        CheckConstraint(
            "status IN ('submitted','paid','partially_shipped','shipped','completed','cancelled')",
            name="ck_sales_orders_status",
        ),
        Index("ix_sales_orders_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id", ondelete="RESTRICT"), nullable=False, index=True)
    salesperson_id: Mapped[int] = mapped_column(ForeignKey("salespersons.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="submitted", nullable=False, index=True)
    order_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    # Current/default address for whatever on this order hasn't shipped yet.
    # Editable while any unshipped quantity remains (see ADDRESS_EDITABLE_STATUSES
    # in sales_order_service.py); each SalesShipment copies its own frozen
    # snapshot from this at the moment the shipment is created.
    recipient_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    recipient_phone_snapshot: Mapped[str | None] = mapped_column(String(50))
    shipping_address_snapshot: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    customer: Mapped[Customer] = relationship(back_populates="sales_orders")
    salesperson: Mapped[Salesperson] = relationship(back_populates="sales_orders")
    items: Mapped[list[SalesOrderItem]] = relationship(
        back_populates="sales_order", cascade="all, delete-orphan", order_by="SalesOrderItem.id",
    )
    shipping_labels: Mapped[list[SalesOrderShippingLabel]] = relationship(
        back_populates="sales_order", cascade="all, delete-orphan", order_by="SalesOrderShippingLabel.created_at",
    )
    shipments: Mapped[list[SalesShipment]] = relationship(
        back_populates="sales_order", cascade="all, delete-orphan", order_by="SalesShipment.id",
    )

    @property
    def total_amount(self) -> Decimal:
        return sum((item.line_amount for item in self.items), Decimal("0"))

    @property
    def total_quantity(self) -> int:
        return sum(item.quantity for item in self.items)

    @property
    def item_kind_count(self) -> int:
        return len(self.items)


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sales_order_items_quantity_positive"),
        CheckConstraint("unit_sale_price >= 0", name="ck_sales_order_items_price_non_negative"),
        CheckConstraint(
            "product_id IS NOT NULL OR product_name_snapshot IS NOT NULL OR manual_image_relative_path IS NOT NULL",
            name="ck_sales_order_items_identity_present",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    # Doubles as the manual item's name for product_id-less rows; nullable so a
    # manual item can be identified by image alone (see the identity CheckConstraint).
    product_name_snapshot: Mapped[str | None] = mapped_column(String(255))
    jan_snapshot: Mapped[str | None] = mapped_column(String(32))
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    # CNY ("微信售价"), hand-entered per order line -- never defaulted from any
    # JPY purchase/sale price field.
    unit_sale_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    # Manual-item photo, a permanent historical snapshot of this order line --
    # never deleted/replaced even if the line is later linked to a real Product.
    manual_image_relative_path: Mapped[str | None] = mapped_column(Text)
    manual_image_original_filename: Mapped[str | None] = mapped_column(String(255))
    manual_image_content_type: Mapped[str | None] = mapped_column(String(100))
    manual_image_file_size: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    sales_order: Mapped[SalesOrder] = relationship(back_populates="items")
    product: Mapped[Product | None] = relationship()
    shipment_items: Mapped[list[SalesShipmentItem]] = relationship(back_populates="sales_order_item")

    @property
    def line_amount(self) -> Decimal:
        return (self.unit_sale_price or Decimal("0")) * self.quantity

    @property
    def shipped_quantity(self) -> int:
        """Quantity confirmed shipped so far (only counts shipments actually
        marked shipped -- a pending, not-yet-shipped shipment does not count
        as fulfilling the order yet)."""
        return sum(
            shipment_item.quantity
            for shipment_item in self.shipment_items
            if shipment_item.shipment.status == "shipped"
        )

    @property
    def remaining_quantity(self) -> int:
        return self.quantity - self.shipped_quantity


class SalesOrderShippingLabel(Base):
    __tablename__ = "sales_order_shipping_labels"
    id: Mapped[int] = mapped_column(primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    # Nullable: pre-shipment-model labels were attached directly to the order;
    # new uploads always attach to the shipment they document.
    shipment_id: Mapped[int | None] = mapped_column(ForeignKey("sales_shipments.id", ondelete="CASCADE"), index=True)
    stored_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255))
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    sales_order: Mapped[SalesOrder] = relationship(back_populates="shipping_labels")
    shipment: Mapped[SalesShipment | None] = relationship(back_populates="shipping_labels")


class SalesShipment(Base):
    """One dispatch event for part or all of a SalesOrder's items.

    A paid order can have any number of shipments over time; each one is an
    immutable historical fact once marked shipped (see mark_shipment_shipped
    in sales_order_service.py) -- its address snapshot and item quantities
    never change again after that point.
    """

    __tablename__ = "sales_shipments"
    __table_args__ = (
        CheckConstraint("status IN ('pending','shipped')", name="ck_sales_shipments_status"),
        Index("ix_sales_shipments_tracking_due", "tracking_terminal", "tracking_next_check_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    sales_order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    shipment_no: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False, index=True)
    recipient_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    recipient_phone_snapshot: Mapped[str | None] = mapped_column(String(50))
    shipping_address_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    carrier: Mapped[str | None] = mapped_column(String(50), default="中通")
    tracking_no: Mapped[str | None] = mapped_column(String(100))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Kuaidi100-sourced domestic (中通-only, Phase 10A) tracking state --
    # separate from `status` above, which is JBA's own dispatch lifecycle and
    # must never be auto-changed by carrier tracking (see
    # app/shipment_tracking_service.py). tracking_status is one of
    # TRACKING_STATUS_LABELS' keys; tracking_terminal is driven solely by the
    # official `ischeck` field (never guessed from Chinese status text).
    tracking_status: Mapped[str | None] = mapped_column(String(20))
    tracking_terminal: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    tracking_last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_last_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    sales_order: Mapped[SalesOrder] = relationship(back_populates="shipments")
    items: Mapped[list[SalesShipmentItem]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", order_by="SalesShipmentItem.id",
    )
    shipping_labels: Mapped[list[SalesOrderShippingLabel]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", order_by="SalesOrderShippingLabel.created_at",
    )
    tracking_events: Mapped[list[ShipmentTrackingEvent]] = relationship(
        back_populates="shipment", cascade="all, delete-orphan", order_by="ShipmentTrackingEvent.event_time.desc()",
    )


class SalesShipmentItem(Base):
    __tablename__ = "sales_shipment_items"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_sales_shipment_items_quantity_positive"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("sales_shipments.id", ondelete="CASCADE"), nullable=False, index=True)
    # RESTRICT: a shipment referencing an order item must never be left
    # dangling -- shipped history is never allowed to lose its item link.
    sales_order_item_id: Mapped[int] = mapped_column(ForeignKey("sales_order_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    shipment: Mapped[SalesShipment] = relationship(back_populates="items")
    sales_order_item: Mapped[SalesOrderItem] = relationship(back_populates="shipment_items")


class ShipmentTrackingEvent(Base):
    """One de-duplicated tracking-history entry from Kuaidi100 for a shipment.

    Kuaidi100 always returns the full history on every query, not just new
    events -- event_hash (over event_time+status+description) is how repeat
    queries avoid inserting the same event twice.
    """

    __tablename__ = "shipment_tracking_events"
    __table_args__ = (
        Index("uq_shipment_tracking_events_dedupe", "shipment_id", "event_hash", unique=True),
        Index("ix_shipment_tracking_events_shipment_time", "shipment_id", "event_time"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    shipment_id: Mapped[int] = mapped_column(ForeignKey("sales_shipments.id", ondelete="CASCADE"), nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    area_code: Mapped[str | None] = mapped_column(String(30))
    area_name: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str | None] = mapped_column(String(50))
    event_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    shipment: Mapped[SalesShipment] = relationship(back_populates="tracking_events")


class ProcurementDemand(Base):
    """One raw procurement-need record from a single source.

    Multiple demands for the same product are never merged into one row --
    aggregation happens dynamically in app.procurement_service so every
    source (who asked, through what channel, for how many) stays traceable.
    """

    __tablename__ = "procurement_demands"
    __table_args__ = (
        CheckConstraint(
            "demand_type IN ('sales_confirmed','channel_shortage','manual_restock','investigation','system_restock')",
            name="ck_procurement_demands_demand_type",
        ),
        CheckConstraint("source_person IN ('秀','丈母娘','老婆','系统')", name="ck_procurement_demands_source_person"),
        CheckConstraint(
            "source_type IN ('sales_order','channel_shortage','manual','investigation','system_restock')",
            name="ck_procurement_demands_source_type",
        ),
        CheckConstraint("status IN ('open','planned','closed','cancelled')", name="ck_procurement_demands_status"),
        CheckConstraint("requested_quantity IS NULL OR requested_quantity > 0", name="ck_procurement_demands_quantity_positive"),
        Index(
            "uq_procurement_demands_sales_order_item", "sales_order_item_id", unique=True,
            sqlite_where=text("sales_order_item_id IS NOT NULL"),
        ),
        Index("ix_procurement_demands_status_type", "status", "demand_type"),
        Index("ix_procurement_demands_product_status", "product_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    jan_snapshot: Mapped[str | None] = mapped_column(String(32))
    demand_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    source_person: Mapped[str] = mapped_column(String(20), nullable=False)
    source_channel: Mapped[str | None] = mapped_column(String(50))
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    sales_order_item_id: Mapped[int | None] = mapped_column(ForeignKey("sales_order_items.id", ondelete="CASCADE"), index=True)
    requested_quantity: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="open", nullable=False, index=True)
    # A manual (no matching Product) demand raised via a photo only -- e.g. a
    # customer sent a picture and nobody knows the JAN/name yet. Reuses the same
    # safe-upload mechanism as sales_order_items' manual images (Phase 7); the
    # image is a permanent historical snapshot, never auto-deleted even once the
    # demand is later linked to a real Product.
    manual_image_relative_path: Mapped[str | None] = mapped_column(Text)
    manual_image_original_filename: Mapped[str | None] = mapped_column(String(255))
    manual_image_content_type: Mapped[str | None] = mapped_column(String(100))
    manual_image_file_size: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    product: Mapped[Product | None] = relationship()
    sales_order_item: Mapped[SalesOrderItem | None] = relationship()
    plan_sources: Mapped[list[ProcurementDemandPlanSource]] = relationship(back_populates="demand")


class ProcurementDemandPlan(Base):
    """A purchasing decision for one product, made by aggregating open demands.

    Phase 2A stops here: no actual purchase quantity/price/receipt tracking yet.
    """

    __tablename__ = "procurement_demand_plans"
    __table_args__ = (
        CheckConstraint("planned_quantity > 0", name="ck_procurement_demand_plans_quantity_positive"),
        CheckConstraint("status IN ('planned','cancelled')", name="ck_procurement_demand_plans_status"),
        Index("ix_procurement_demand_plans_status_created", "status", "created_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="SET NULL"), index=True)
    product_name_snapshot: Mapped[str] = mapped_column(String(255), nullable=False)
    jan_snapshot: Mapped[str | None] = mapped_column(String(32))
    planned_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_demand_quantity_snapshot: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="planned", nullable=False, index=True)
    created_by: Mapped[str | None] = mapped_column(String(20))
    note: Mapped[str | None] = mapped_column(Text)
    # Phase 2C: where 老婆 decided to actually buy this -- a decision fact, set
    # only by her explicit choice. Never populated from a recommendation; system
    # suggestions are computed on the fly in procurement_service and never written
    # here. Recommendations are computed on the fly (not stored) because they
    # would go stale the moment new purchase history comes in.
    selected_store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    product: Mapped[Product | None] = relationship()
    selected_store: Mapped[Store | None] = relationship()
    sources: Mapped[list[ProcurementDemandPlanSource]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="ProcurementDemandPlanSource.id",
    )
    executions: Mapped[list[ProcurementPurchaseExecution]] = relationship(
        back_populates="plan", cascade="all, delete-orphan", order_by="ProcurementPurchaseExecution.created_at",
    )


class ProcurementPurchaseExecution(Base):
    """One real trip's worth of actually-bought quantity for a plan -- not yet a formal PurchaseBatch.

    A plan can have several of these over time (partial buys, different stores
    on different visits). planned_quantity on the plan never changes; purchased/
    remaining are always computed by summing non-cancelled executions.
    """

    __tablename__ = "procurement_purchase_executions"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="ck_procurement_purchase_executions_quantity_positive"),
        CheckConstraint(
            "status IN ('pending_receipt','reconciled','cancelled')", name="ck_procurement_purchase_executions_status",
        ),
        Index("ix_procurement_purchase_executions_plan_status", "plan_id", "status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("procurement_demand_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    # Snapshot, not a live pointer: plan.selected_store_id can change later without
    # rewriting where a past execution actually happened. SET NULL (not RESTRICT)
    # so deleting a Store can never block/cascade-delete real purchase history.
    store_id: Mapped[int | None] = mapped_column(ForeignKey("stores.id", ondelete="SET NULL"), index=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending_receipt", nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    plan: Mapped[ProcurementDemandPlan] = relationship(back_populates="executions")
    store: Mapped[Store | None] = relationship()
    receipt_matches: Mapped[list[ProcurementExecutionReceiptMatch]] = relationship(
        back_populates="execution", cascade="all, delete-orphan", order_by="ProcurementExecutionReceiptMatch.id",
    )


class ProcurementExecutionReceiptMatch(Base):
    """A human-confirmed link: this much of a receipt line is this execution's real evidence.

    Written only once reconciliation is confirmed as part of the existing
    receipt confirm transaction (Phase 2E) -- there is no separate "suggested"
    row and nothing here is ever auto-created. The link to the eventual
    PurchaseBatchItem is derived through receipt_item_id (PurchaseBatchItem
    already has a 1:1 receipt_item_id), so it isn't duplicated here.
    """

    __tablename__ = "procurement_execution_receipt_matches"
    __table_args__ = (
        CheckConstraint("matched_quantity > 0", name="ck_procurement_execution_receipt_matches_quantity_positive"),
        UniqueConstraint("execution_id", "receipt_item_id", name="uq_procurement_execution_receipt_matches_pair"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    execution_id: Mapped[int] = mapped_column(
        ForeignKey("procurement_purchase_executions.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    receipt_item_id: Mapped[int] = mapped_column(ForeignKey("receipt_items.id", ondelete="RESTRICT"), nullable=False, index=True)
    matched_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    execution: Mapped[ProcurementPurchaseExecution] = relationship(back_populates="receipt_matches")
    receipt_item: Mapped[ReceiptItem] = relationship()


class ProcurementDemandPlanSource(Base):
    """Links a plan back to every demand it was built from, so 'why we're buying this' never gets lost."""

    __tablename__ = "procurement_demand_plan_sources"
    __table_args__ = (UniqueConstraint("plan_id", "demand_id", name="uq_procurement_demand_plan_sources_plan_demand"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("procurement_demand_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    demand_id: Mapped[int] = mapped_column(ForeignKey("procurement_demands.id", ondelete="RESTRICT"), nullable=False, index=True)
    quantity_snapshot: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    plan: Mapped[ProcurementDemandPlan] = relationship(back_populates="sources")
    demand: Mapped[ProcurementDemand] = relationship(back_populates="plan_sources")


from app.product_identity import install_product_identity_events

install_product_identity_events()
