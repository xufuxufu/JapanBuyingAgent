from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint, text
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
    main_image_path: Mapped[str | None] = mapped_column(Text)
    main_image_source_url: Mapped[str | None] = mapped_column(Text)
    product_data_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    name_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    main_image_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    main_image_source_platform: Mapped[str | None] = mapped_column(String(50))
    main_image_downloaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    main_image_hash: Mapped[str | None] = mapped_column(String(64))
    brand: Mapped[str | None] = mapped_column(String(128))
    manufacturer: Mapped[str | None] = mapped_column(String(128))
    category: Mapped[str | None] = mapped_column(String(128))
    capacity: Mapped[str | None] = mapped_column(String(64))
    color: Mapped[str | None] = mapped_column(String(64))
    model_number: Mapped[str | None] = mapped_column(String(128))
    package_count: Mapped[str | None] = mapped_column(String(64))
    specification: Mapped[str | None] = mapped_column(String(255))
    model_spec: Mapped[str | None] = mapped_column(String(255))
    purchase_price: Mapped[int | None] = mapped_column(Integer)
    sale_price: Mapped[int | None] = mapped_column(Integer)
    minimum_sale_price: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(Text)
    location_code: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    source: Mapped[str] = mapped_column(String(50), default="manual", nullable=False)
    product_origin: Mapped[str] = mapped_column(String(20), default="manual", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    purchase_details: Mapped[list[PurchaseBatchItem]] = relationship(back_populates="product")
    price_search_runs: Mapped[list[PriceSearchRun]] = relationship(back_populates="product")
    watch_config: Mapped[ProductWatchConfig | None] = relationship(back_populates="product", uselist=False)
    watch_recommendations: Mapped[list[ProductWatchRecommendation]] = relationship(back_populates="product")


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

    @property
    def display_name(self) -> str:
        cn = self.name_cn or (self.name if not self.name_ja else None) or "中文名待补"
        return f"{cn}｜{self.name_ja or '日文名待补'}"


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
    qinsi_target_warehouse_id: Mapped[int] = mapped_column(ForeignKey("locations.id", ondelete="RESTRICT"), nullable=False)
    parent_export_job_id: Mapped[int | None] = mapped_column(ForeignKey("qinsi_purchase_export_jobs.id", ondelete="RESTRICT"), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="generated", nullable=False, index=True)
    line_count: Mapped[int] = mapped_column(Integer, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    purchase_batch: Mapped[PurchaseBatch] = relationship(back_populates="qinsi_export_jobs")
    qinsi_target_warehouse: Mapped[Location] = relationship()
    parent_export_job: Mapped[QinsiPurchaseExportJob | None] = relationship(remote_side=[id])
    lines: Mapped[list[QinsiPurchaseExportLine]] = relationship(
        back_populates="export_job", cascade="all, delete-orphan", order_by="QinsiPurchaseExportLine.row_no",
    )


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


class InventoryTransaction(Base):
    __tablename__ = "inventory_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id", ondelete="RESTRICT"))
    receipt_item_id: Mapped[int | None] = mapped_column(ForeignKey("receipt_items.id", ondelete="RESTRICT"))
    transaction_type: Mapped[str] = mapped_column(String(30), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_cost: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


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
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    product: Mapped[Product] = relationship(back_populates="watch_config")


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


from app.product_identity import install_product_identity_events

install_product_identity_events()
