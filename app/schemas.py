from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_name: str = ""
    purchased_at: datetime | None = None
    receipt_number: str | None = None
    store_code: str | None = None
    phone: str | None = None
    postal_code: str | None = None
    address: str | None = None
    branch_name: str | None = None


class TotalsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subtotal: int | None
    discount_total: int
    tax_total: int | None
    paid_total: int | None


class ItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_file: str | None = None
    source_page_no: int | None = Field(default=None, ge=1)
    line_no: int = Field(ge=1)
    raw_name: str
    recognized_name: str | None
    jan_candidate: str | None
    quantity: int = Field(ge=1)
    unit_price: int | None
    discount_amount: int
    tax_rate: float | None
    line_total: int | None
    confidence: float = Field(ge=0, le=1)

    @field_validator("recognized_name", mode="before")
    @classmethod
    def normalize_recognized_name(cls, value):
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("jan_candidate", mode="before")
    @classmethod
    def preserve_jan_as_string(cls, value):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("jan_candidate 必须是字符串，以保留前导零")
        return value


class RecognitionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    source_file: str | None = None
    source_page_no: int | None = Field(default=None, ge=1)
    store: StoreInput
    totals: TotalsInput
    items: list[ItemInput]
    warnings: list[str] = []

    @field_validator("schema_version")
    @classmethod
    def supported_schema(cls, value: str) -> str:
        if value not in {"1.0", "1.1"}:
            raise ValueError("仅支持 schema_version 1.0 或 1.1")
        return value

    @field_validator("items")
    @classmethod
    def unique_line_numbers(cls, value: list[ItemInput]) -> list[ItemInput]:
        numbers = [item.line_no for item in value]
        if len(numbers) != len(set(numbers)):
            raise ValueError("items.line_no 必须唯一")
        return value

    @model_validator(mode="after")
    def require_sources_in_v11(self):
        if self.schema_version == "1.1":
            if not self.source_file or self.source_page_no is None:
                raise ValueError("schema 1.1 的小票必须包含 source_file 和 source_page_no")
            if any(not item.source_file or item.source_page_no is None for item in self.items):
                raise ValueError("schema 1.1 的每条商品必须包含 source_file 和 source_page_no")
        return self


class BatchStoreInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    raw_name: str
    store_code: str | None = None
    phone: str | None = None
    postal_code: str | None = None
    address: str | None = None
    branch_name: str | None = None


class BatchReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_file: str
    source_page_no: int = Field(ge=1)
    store: BatchStoreInput
    purchased_at: datetime | None
    receipt_number: str | None
    totals: TotalsInput
    items: list[ItemInput]
    warnings: list[str]

    @field_validator("items")
    @classmethod
    def validate_items(cls, value: list[ItemInput]) -> list[ItemInput]:
        numbers = [item.line_no for item in value]
        if len(numbers) != len(set(numbers)):
            raise ValueError("items.line_no 必须唯一")
        if any(not item.source_file or item.source_page_no is None for item in value):
            raise ValueError("schema 1.1 的每条商品必须包含 source_file 和 source_page_no")
        return value

    @model_validator(mode="after")
    def item_sources_match_receipt(self):
        for item in self.items:
            if item.source_file != self.source_file:
                raise ValueError("item.source_file 必须与所属 receipt.source_file 完全一致")
            if item.source_page_no != self.source_page_no:
                raise ValueError("item.source_page_no 必须与所属 receipt.source_page_no 一致")
        return self


class RecognitionBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str
    receipts: list[BatchReceiptInput] = Field(min_length=1)

    @field_validator("schema_version")
    @classmethod
    def require_v11(cls, value: str) -> str:
        if value != "1.1":
            raise ValueError("GPT 识别任务批量导入仅支持 schema_version 1.1")
        return value


class ReceiptDraftInput(BaseModel):
    raw_store_name: str = ""
    raw_store_code: str = ""
    raw_store_phone: str = ""
    raw_store_postal_code: str = ""
    raw_store_address: str = ""
    raw_store_branch_name: str = ""
    purchased_at: datetime | None = None
    receipt_number: str = ""
    subtotal: int | None = None
    discount_total: int = 0
    tax_total: int | None = None
    paid_total: int | None = None


class StoreBrandCreateInput(BaseModel):
    name_cn: str | None = Field(default=None, max_length=128)
    name_ja: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_name(self):
        if not (self.name_cn and self.name_cn.strip()) and not (self.name_ja and self.name_ja.strip()):
            raise ValueError("店铺品牌至少填写中文名或日文名")
        return self


class StoreCreateInput(BaseModel):
    brand_id: int | None = Field(default=None, ge=1)
    name_cn: str | None = Field(default=None, max_length=128)
    name_ja: str | None = Field(default=None, max_length=128)
    raw_name: str | None = Field(default=None, max_length=255)
    phone: str | None = Field(default=None, max_length=50)
    postal_code: str | None = Field(default=None, max_length=20)
    address: str | None = None
    receipt_store_code: str | None = Field(default=None, max_length=100)
    is_active: bool = True
    is_online: bool = False

    @model_validator(mode="after")
    def require_name(self):
        if not any(value and value.strip() for value in (self.name_cn, self.name_ja, self.raw_name)):
            raise ValueError("具体门店至少填写中文名、日文名或原始名称")
        return self


class ReceiptItemDraftInput(BaseModel):
    raw_name: str = Field(min_length=1)
    recognized_name: str = ""
    jan_candidate: str | None = None
    quantity: int = Field(ge=1)
    unit_price: int | None = None
    discount_amount: int = 0
    tax_rate: float | None = None
    line_total: int | None = None
    confidence: float = Field(ge=0, le=1)
    review_status: str = "pending"

    @field_validator("jan_candidate", mode="before")
    @classmethod
    def validate_jan_text(cls, value):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("JAN 必须是字符串")
        return value

    @field_validator("review_status")
    @classmethod
    def validate_review_status(cls, value: str) -> str:
        if value not in {"pending", "reviewed", "ignored"}:
            raise ValueError("review_status 无效")
        return value


class ProductCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jan: str | None = None
    qinsi_product_code: str | None = None
    name_cn: str = Field(min_length=1, max_length=128)

    @field_validator("jan", "qinsi_product_code", mode="before")
    @classmethod
    def validate_identifiers(cls, value):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("商品标识必须是字符串，以保留前导零")
        return value.strip() or None


class ProductUpdateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jan: str | None = None
    qinsi_product_code: str | None = None
    name_cn: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("jan", "qinsi_product_code", mode="before")
    @classmethod
    def validate_identifiers(cls, value):
        if value is None or value == "":
            return None
        if not isinstance(value, str):
            raise ValueError("商品标识必须是字符串，以保留前导零")
        return value.strip() or None


class ProductOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    internal_sku: str
    jan: str | None
    qinsi_product_code: str | None
    name_cn: str | None
    name_ja: str | None
    display_name: str | None
    main_image_path: str | None
    main_image_source_url: str | None
    product_data_confirmed: bool
    product_origin: str


class LocationOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    internal_code: str
    display_name: str
    location_type: Literal["qinsi_warehouse", "local_physical", "transit", "system_status"]
    is_qinsi_warehouse: bool
    is_active: bool
    sort_order: int


class PurchaseConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    initial_location_id: int | None = Field(default=None, ge=1)
    qinsi_target_warehouse_id: int | None = Field(default=None, ge=1)
    line_qinsi_target_overrides: dict[int, int] = Field(default_factory=dict)

    @field_validator("line_qinsi_target_overrides")
    @classmethod
    def validate_override_ids(cls, value: dict[int, int]) -> dict[int, int]:
        if any(item_id < 1 or location_id < 1 for item_id, location_id in value.items()):
            raise ValueError("商品行和目标仓库ID必须为正整数")
        return value


class PurchaseBatchOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    batch_no: str
    receipt_id: int
    gpt_batch_id: int
    purchased_at: datetime | None
    store_name: str | None
    confirmed_at: datetime
    status: Literal["confirmed", "pending_qinsi_submission", "cancelled"]


class QinsiExportConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result: Literal["all_success", "partial_failure", "all_failed"]
    failed_line_ids: set[int] = Field(default_factory=set)

    @field_validator("failed_line_ids")
    @classmethod
    def validate_failed_line_ids(cls, value: set[int]) -> set[int]:
        if any(line_id < 1 for line_id in value):
            raise ValueError("失败行ID必须为正整数")
        return value

    @model_validator(mode="after")
    def validate_result_selection(self):
        if self.result == "partial_failure" and not self.failed_line_ids:
            raise ValueError("部分失败必须至少选择一条失败行")
        if self.result != "partial_failure" and self.failed_line_ids:
            raise ValueError("仅部分失败允许指定失败行")
        return self


class PriceLookupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    jan: str
    current_store_price: int | None = Field(default=None, ge=0)
    force_refresh: bool = False

    @field_validator("jan", mode="before")
    @classmethod
    def validate_jan(cls, value):
        if not isinstance(value, str):
            raise ValueError("JAN 必须是字符串")
        jan = value.strip()
        if not jan.isdigit() or len(jan) not in {8, 12, 13, 14}:
            raise ValueError("JAN 必须为 8、12、13 或 14 位数字")
        digits = [int(char) for char in jan]
        weighted = sum(digit * (3 if (len(digits) - index) % 2 == 0 else 1) for index, digit in enumerate(digits[:-1]))
        if (10 - weighted % 10) % 10 != digits[-1]:
            raise ValueError("JAN 校验位不正确")
        return jan
