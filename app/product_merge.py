from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    FieldPurchaseItem,
    ImportRow,
    InventoryTransaction,
    PriceLookupHistory,
    PriceSearchRun,
    Product,
    ProductAlias,
    ProductBarcode,
    ProductEnrichmentTask,
    ProductMatchLog,
    ProductOffer,
    ProductPlaceholderCleanupLog,
    ProductSerial,
    ProductWatchConfig,
    ProductWatchNotification,
    ProductWatchRecommendation,
    ProductWatchSnapshot,
    PurchaseBatchItem,
    QinsiExportLine,
    QinsiExportLineSource,
    QinsiGoodsImportRow,
    QinsiInventorySnapshotLine,
    QinsiProductMapping,
    QinsiPurchaseExportLine,
    ReceiptItem,
    RestockListItem,
)


MERGE_REASON = "duplicate_jan_merge"


DIRECT_PRODUCT_LINK_MODELS = (
    ReceiptItem,
    PurchaseBatchItem,
    QinsiPurchaseExportLine,
    QinsiInventorySnapshotLine,
    QinsiGoodsImportRow,
    ImportRow,
    InventoryTransaction,
    PriceLookupHistory,
    PriceSearchRun,
    ProductEnrichmentTask,
    ProductOffer,
    ProductBarcode,
    ProductSerial,
    ProductWatchSnapshot,
    ProductWatchNotification,
)


COUNT_MODELS = DIRECT_PRODUCT_LINK_MODELS + (
    ProductAlias,
    QinsiExportLine,
    QinsiProductMapping,
    RestockListItem,
    FieldPurchaseItem,
    ProductWatchConfig,
    ProductWatchRecommendation,
)


@dataclass(frozen=True, slots=True)
class DuplicateJanProductView:
    product: Product
    priority: int
    association_count: int
    is_recommended_primary: bool


@dataclass(frozen=True, slots=True)
class DuplicateJanGroup:
    jan: str
    products: list[DuplicateJanProductView]
    recommended_primary: Product
    association_total: int
    has_qinsi_product: bool


@dataclass(frozen=True, slots=True)
class ProductMergeResult:
    jan: str
    primary_product_id: int
    merged_product_ids: list[int]
    migrated_counts: dict[str, int]
    deleted_product_ids: list[int]
    archived_product_ids: list[int]

    @property
    def migrated_association_count(self) -> int:
        return sum(self.migrated_counts.values())


def is_qinsi_product(product: Product) -> bool:
    return product.product_origin == "qinsi" or product.status == "qinsi_product_imported"


def product_merge_priority(product: Product) -> int:
    if is_qinsi_product(product):
        return 0
    if product.status == "active":
        return 1
    if (product.status or "").startswith("new_pending_"):
        return 2
    return 3


def _primary_sort_key(product: Product) -> tuple[int, int, int, int]:
    return (
        product_merge_priority(product),
        0 if product.status == "qinsi_product_imported" else 1,
        0 if product.qinsi_product_code else 1,
        product.id,
    )


def recommended_primary(products: list[Product]) -> Product:
    if not products:
        raise ValueError("没有可合并商品")
    return sorted(products, key=_primary_sort_key)[0]


def _count(session: Session, model, product_id: int) -> int:
    return session.scalar(select(func.count()).select_from(model).where(model.product_id == product_id)) or 0


def association_counts(session: Session, product_id: int) -> dict[str, int]:
    counts = {model.__tablename__: _count(session, model, product_id) for model in COUNT_MODELS}
    counts["product_match_logs.old_product_id"] = session.scalar(
        select(func.count()).select_from(ProductMatchLog).where(ProductMatchLog.old_product_id == product_id)
    ) or 0
    counts["product_match_logs.new_product_id"] = session.scalar(
        select(func.count()).select_from(ProductMatchLog).where(ProductMatchLog.new_product_id == product_id)
    ) or 0
    return counts


def business_association_count(session: Session, product_id: int) -> int:
    return sum(association_counts(session, product_id).values())


def list_duplicate_jan_groups(session: Session) -> list[DuplicateJanGroup]:
    duplicate_jans = list(session.scalars(
        select(Product.jan)
        .where(Product.jan.is_not(None))
        .group_by(Product.jan)
        .having(func.count(Product.id) > 1)
        .order_by(Product.jan)
    ))
    groups: list[DuplicateJanGroup] = []
    for jan in duplicate_jans:
        products = list(session.scalars(select(Product).where(Product.jan == jan).order_by(Product.id)))
        primary = recommended_primary(products)
        views = [
            DuplicateJanProductView(
                product=product,
                priority=product_merge_priority(product),
                association_count=business_association_count(session, product.id),
                is_recommended_primary=product.id == primary.id,
            )
            for product in products
        ]
        groups.append(DuplicateJanGroup(
            jan=jan or "",
            products=views,
            recommended_primary=primary,
            association_total=sum(view.association_count for view in views),
            has_qinsi_product=any(is_qinsi_product(product) for product in products),
        ))
    return groups


def _bump(counts: dict[str, int], key: str, value: int | None) -> None:
    if value:
        counts[key] = counts.get(key, 0) + value


def _delete_duplicate_aliases(session: Session, old_product_id: int, primary_product_id: int, counts: dict[str, int]) -> None:
    target_aliases = set(session.scalars(
        select(ProductAlias.normalized_alias).where(ProductAlias.product_id == primary_product_id)
    ))
    for alias in list(session.scalars(select(ProductAlias).where(ProductAlias.product_id == old_product_id))):
        if alias.normalized_alias in target_aliases:
            session.delete(alias)
            _bump(counts, "product_aliases.deleted_duplicate", 1)
        else:
            alias.product_id = primary_product_id
            target_aliases.add(alias.normalized_alias)
            _bump(counts, "product_aliases", 1)


def _merge_qinsi_export_lines(session: Session, old_product_id: int, primary: Product, counts: dict[str, int]) -> None:
    old_lines = list(session.scalars(select(QinsiExportLine).where(QinsiExportLine.product_id == old_product_id)))
    for line in old_lines:
        target = session.scalar(select(QinsiExportLine).where(
            QinsiExportLine.job_id == line.job_id,
            QinsiExportLine.product_id == primary.id,
        ))
        if target is None:
            line.product_id = primary.id
            line.qinsi_product_code = primary.qinsi_product_code or line.qinsi_product_code
            line.product_name = primary.display_name or primary.name_cn or primary.name_ja or line.product_name
            _bump(counts, "qinsi_export_lines", 1)
        else:
            target.quantity += line.quantity
            session.execute(update(QinsiExportLineSource).where(QinsiExportLineSource.export_line_id == line.id).values(export_line_id=target.id))
            session.delete(line)
            _bump(counts, "qinsi_export_lines.merged_duplicate", 1)


def _merge_qinsi_mappings(session: Session, old_product_id: int, primary_product_id: int, counts: dict[str, int]) -> None:
    for mapping in list(session.scalars(select(QinsiProductMapping).where(QinsiProductMapping.product_id == old_product_id))):
        target = session.scalar(select(QinsiProductMapping).where(QinsiProductMapping.qinsi_product_code == mapping.qinsi_product_code))
        if target is not None and target.id != mapping.id:
            session.delete(mapping)
            _bump(counts, "qinsi_product_mappings.deleted_duplicate", 1)
        else:
            mapping.product_id = primary_product_id
            _bump(counts, "qinsi_product_mappings", 1)


def _merge_restock_items(session: Session, old_product_id: int, primary_product_id: int, counts: dict[str, int]) -> None:
    for item in list(session.scalars(select(RestockListItem).where(RestockListItem.product_id == old_product_id))):
        target = session.scalar(select(RestockListItem).where(
            RestockListItem.restock_list_id == item.restock_list_id,
            RestockListItem.product_id == primary_product_id,
        ))
        if target is None:
            item.product_id = primary_product_id
            _bump(counts, "restock_list_items", 1)
        else:
            session.delete(item)
            _bump(counts, "restock_list_items.deleted_duplicate", 1)


def _merge_field_purchase_items(session: Session, old_product_id: int, primary_product_id: int, counts: dict[str, int]) -> None:
    for item in list(session.scalars(select(FieldPurchaseItem).where(FieldPurchaseItem.product_id == old_product_id))):
        target = session.scalar(select(FieldPurchaseItem).where(
            FieldPurchaseItem.batch_id == item.batch_id,
            FieldPurchaseItem.product_id == primary_product_id,
        ))
        if target is None:
            item.product_id = primary_product_id
            _bump(counts, "field_purchase_items", 1)
        else:
            target.quantity += item.quantity
            session.delete(item)
            _bump(counts, "field_purchase_items.merged_duplicate", 1)


def _merge_watch_rows(session: Session, old_product_id: int, primary_product_id: int, counts: dict[str, int]) -> None:
    primary_config = session.scalar(select(ProductWatchConfig).where(ProductWatchConfig.product_id == primary_product_id))
    old_config = session.scalar(select(ProductWatchConfig).where(ProductWatchConfig.product_id == old_product_id))
    if old_config is not None and primary_config is not None:
        session.execute(update(ProductWatchSnapshot).where(ProductWatchSnapshot.watch_config_id == old_config.id).values(
            watch_config_id=primary_config.id,
            product_id=primary_product_id,
        ))
        session.execute(update(ProductWatchNotification).where(ProductWatchNotification.watch_config_id == old_config.id).values(
            watch_config_id=primary_config.id,
            product_id=primary_product_id,
        ))
        session.delete(old_config)
        _bump(counts, "product_watch_configs.deleted_duplicate", 1)
    elif old_config is not None:
        old_config.product_id = primary_product_id
        _bump(counts, "product_watch_configs", 1)

    for recommendation in list(session.scalars(select(ProductWatchRecommendation).where(ProductWatchRecommendation.product_id == old_product_id))):
        target = session.scalar(select(ProductWatchRecommendation).where(
            ProductWatchRecommendation.product_id == primary_product_id,
            ProductWatchRecommendation.reason == recommendation.reason,
            ProductWatchRecommendation.accepted.is_(False),
            ProductWatchRecommendation.ignored.is_(False),
        ))
        if target is not None and not recommendation.accepted and not recommendation.ignored:
            session.delete(recommendation)
            _bump(counts, "product_watch_recommendations.deleted_duplicate", 1)
        else:
            recommendation.product_id = primary_product_id
            _bump(counts, "product_watch_recommendations", 1)


def _update_direct_links(session: Session, old_product_id: int, primary_product_id: int, counts: dict[str, int]) -> None:
    for model in DIRECT_PRODUCT_LINK_MODELS:
        result = session.execute(update(model).where(model.product_id == old_product_id).values(product_id=primary_product_id))
        _bump(counts, model.__tablename__, result.rowcount)
    for column in (ProductMatchLog.old_product_id, ProductMatchLog.new_product_id):
        result = session.execute(update(ProductMatchLog).where(column == old_product_id).values({column.key: primary_product_id}))
        _bump(counts, f"product_match_logs.{column.key}", result.rowcount)


def _snapshot(product: Product) -> dict:
    return {
        "id": product.id,
        "internal_sku": product.internal_sku,
        "jan": product.jan,
        "qinsi_product_code": product.qinsi_product_code,
        "status": product.status,
        "product_origin": product.product_origin,
        "source": product.source,
        "display_name": product.display_name,
        "name_cn": product.name_cn,
        "name_ja": product.name_ja,
    }


def _clear_or_archive_old_product(session: Session, old_product: Product, primary: Product, counts: dict[str, int], actor: str) -> tuple[bool, bool]:
    old_snapshot = _snapshot(old_product)
    old_product.jan = None
    old_product.qinsi_product_code = None
    old_product.status = "archived"
    old_product.needs_review = True
    session.flush()
    remaining = business_association_count(session, old_product.id)
    if remaining == 0:
        if hasattr(ProductPlaceholderCleanupLog, "__tablename__"):
            session.add(ProductPlaceholderCleanupLog(
                old_product_id=old_product.id,
                new_product_id=primary.id,
                jan=old_snapshot.get("jan") or "",
                migrated_association_count=sum(counts.values()),
                operation_type=MERGE_REASON,
                actor=actor,
                reason=MERGE_REASON,
            ))
            session.flush()
        session.delete(old_product)
        return True, False
    if hasattr(ProductPlaceholderCleanupLog, "__tablename__"):
        session.add(ProductPlaceholderCleanupLog(
            old_product_id=old_product.id,
            new_product_id=primary.id,
            jan=old_snapshot.get("jan") or "",
            migrated_association_count=sum(counts.values()),
            operation_type=f"{MERGE_REASON}_archived",
            actor=actor,
            reason=MERGE_REASON,
        ))
    return False, True


def merge_product_into(session: Session, old_product: Product, primary: Product, *, actor: str = "system") -> ProductMergeResult:
    if old_product.id == primary.id:
        raise ValueError("不能将商品合并到自身")
    if not old_product.jan or (primary.jan is not None and old_product.jan != primary.jan):
        raise ValueError("只能合并同 JAN 商品")
    merge_jan = primary.jan or old_product.jan
    actor = (actor or "system").strip()[:128] or "system"
    counts: dict[str, int] = {}
    try:
        _delete_duplicate_aliases(session, old_product.id, primary.id, counts)
        _merge_qinsi_export_lines(session, old_product.id, primary, counts)
        _merge_qinsi_mappings(session, old_product.id, primary.id, counts)
        _merge_restock_items(session, old_product.id, primary.id, counts)
        _merge_field_purchase_items(session, old_product.id, primary.id, counts)
        _merge_watch_rows(session, old_product.id, primary.id, counts)
        _update_direct_links(session, old_product.id, primary.id, counts)
        session.flush()
        deleted, archived = _clear_or_archive_old_product(session, old_product, primary, counts, actor)
        session.flush()
    except IntegrityError as exc:
        raise ValueError(f"合并商品 {old_product.id} 到 {primary.id} 失败：存在唯一约束冲突") from exc
    return ProductMergeResult(
        jan=merge_jan or "",
        primary_product_id=primary.id,
        merged_product_ids=[old_product.id],
        migrated_counts=counts,
        deleted_product_ids=[old_product.id] if deleted else [],
        archived_product_ids=[old_product.id] if archived else [],
    )


def merge_duplicate_jan_group(
    session: Session,
    jan: str,
    *,
    primary_product_id: int | None = None,
    actor: str = "system",
    require_qinsi_primary: bool = True,
    commit: bool = True,
) -> ProductMergeResult | None:
    products = list(session.scalars(select(Product).where(Product.jan == jan).order_by(Product.id)))
    if len(products) <= 1:
        return None
    primary = session.get(Product, primary_product_id) if primary_product_id is not None else recommended_primary(products)
    if primary is None or primary.jan != jan:
        raise ValueError("主商品不属于当前重复 JAN")
    if require_qinsi_primary and any(is_qinsi_product(product) for product in products) and not is_qinsi_product(primary):
        raise ValueError("存在秦丝商品时必须合并到秦丝商品")
    aggregate = ProductMergeResult(jan=jan, primary_product_id=primary.id, merged_product_ids=[], migrated_counts={}, deleted_product_ids=[], archived_product_ids=[])
    try:
        for product in products:
            if product.id == primary.id:
                continue
            result = merge_product_into(session, product, primary, actor=actor)
            aggregate.merged_product_ids.extend(result.merged_product_ids)
            aggregate.deleted_product_ids.extend(result.deleted_product_ids)
            aggregate.archived_product_ids.extend(result.archived_product_ids)
            for key, value in result.migrated_counts.items():
                aggregate.migrated_counts[key] = aggregate.migrated_counts.get(key, 0) + value
        primary.updated_at = datetime.now(timezone.utc)
        if commit:
            session.commit()
    except Exception:
        if commit:
            session.rollback()
        raise
    return aggregate


def merge_products_with_jan_into_primary(
    session: Session,
    jan: str,
    primary: Product,
    *,
    actor: str = "system",
    commit: bool = True,
) -> ProductMergeResult | None:
    products = list(session.scalars(select(Product).where(Product.jan == jan, Product.id != primary.id).order_by(Product.id)))
    if not products:
        return None
    if primary.jan not in {None, jan}:
        raise ValueError("主商品已有其他 JAN，不能自动合并")
    if any(is_qinsi_product(product) for product in products) and not is_qinsi_product(primary):
        raise ValueError("存在秦丝商品时必须合并到秦丝商品")
    aggregate = ProductMergeResult(jan=jan, primary_product_id=primary.id, merged_product_ids=[], migrated_counts={}, deleted_product_ids=[], archived_product_ids=[])
    try:
        for product in products:
            result = merge_product_into(session, product, primary, actor=actor)
            aggregate.merged_product_ids.extend(result.merged_product_ids)
            aggregate.deleted_product_ids.extend(result.deleted_product_ids)
            aggregate.archived_product_ids.extend(result.archived_product_ids)
            for key, value in result.migrated_counts.items():
                aggregate.migrated_counts[key] = aggregate.migrated_counts.get(key, 0) + value
        primary.jan = jan
        primary.has_jan = True
        primary.updated_at = datetime.now(timezone.utc)
        if commit:
            session.commit()
    except Exception:
        if commit:
            session.rollback()
        raise
    return aggregate


def merge_all_duplicate_jans(session: Session, *, actor: str = "system", commit: bool = True) -> list[ProductMergeResult]:
    results: list[ProductMergeResult] = []
    try:
        for group in list_duplicate_jan_groups(session):
            result = merge_duplicate_jan_group(
                session,
                group.jan,
                primary_product_id=group.recommended_primary.id,
                actor=actor,
                commit=False,
            )
            if result is not None:
                results.append(result)
        if commit:
            session.commit()
    except Exception:
        if commit:
            session.rollback()
        raise
    return results


def merge_summary_json(result: ProductMergeResult) -> str:
    return json.dumps({
        "jan": result.jan,
        "primary_product_id": result.primary_product_id,
        "merged_product_ids": result.merged_product_ids,
        "migrated_counts": result.migrated_counts,
        "deleted_product_ids": result.deleted_product_ids,
        "archived_product_ids": result.archived_product_ids,
    }, ensure_ascii=False)
