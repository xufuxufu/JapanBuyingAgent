from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Location


DEFAULT_PHYSICAL_LOCATION_CODE = "LOC-JP-HOME"
QINSI_NO_BARCODE_LOCATION_CODE = "QW-NO-BARCODE"
QINSI_NEW_JAPAN_WAREHOUSE_CODE = "QW-NEW-JAPAN"


@dataclass(frozen=True, slots=True)
class DefaultLocation:
    internal_code: str
    display_name: str
    location_type: str
    is_qinsi_warehouse: bool
    sort_order: int


DEFAULT_LOCATIONS = (
    DefaultLocation("QW-2025-QIANYU", "2025千羽", "qinsi_warehouse", True, 10),
    DefaultLocation("QW-2025-ZHAOCAIMAO", "2025招财猫", "qinsi_warehouse", True, 20),
    DefaultLocation("QW-NO-BARCODE", "无条码商品", "qinsi_warehouse", True, 30),
    DefaultLocation(QINSI_NEW_JAPAN_WAREHOUSE_CODE, "新日本仓库", "qinsi_warehouse", True, 40),
    DefaultLocation(DEFAULT_PHYSICAL_LOCATION_CODE, "日本家里库存", "local_physical", True, 50),
    DefaultLocation("TRANSIT-INTERNATIONAL", "国际快递在途", "transit", False, 100),
    DefaultLocation("TRANSIT-HAND-CARRY", "人工带货在途", "transit", False, 110),
    DefaultLocation("STATUS-UNASSIGNED", "待分配", "system_status", False, 120),
    DefaultLocation("STATUS-QINSI-HANDOFF", "已交接秦丝", "system_status", False, 130),
)


def initialize_default_locations(session: Session, *, commit: bool = True) -> list[Location]:
    existing = {
        location.internal_code: location
        for location in session.scalars(select(Location).where(Location.internal_code.in_(item.internal_code for item in DEFAULT_LOCATIONS)))
    }
    for item in DEFAULT_LOCATIONS:
        if item.internal_code not in existing:
            location = Location(
                internal_code=item.internal_code,
                display_name=item.display_name,
                location_type=item.location_type,
                is_qinsi_warehouse=item.is_qinsi_warehouse,
                is_active=True,
                sort_order=item.sort_order,
            )
            session.add(location)
            existing[item.internal_code] = location
    if commit:
        session.commit()
    else:
        session.flush()
    return list_locations(session)


def list_locations(session: Session, *, active_only: bool = False) -> list[Location]:
    query = select(Location)
    if active_only:
        query = query.where(Location.is_active.is_(True))
    return list(session.scalars(query.order_by(Location.sort_order, Location.id)))


def get_default_physical_location(session: Session) -> Location:
    location = session.scalar(select(Location).where(Location.internal_code == DEFAULT_PHYSICAL_LOCATION_CODE))
    if location is None:
        raise LookupError("默认物理位置“日本家里库存”不存在，请先执行位置初始化")
    return location


def get_location_by_code(session: Session, internal_code: str) -> Location:
    location = session.scalar(select(Location).where(Location.internal_code == internal_code))
    if location is None:
        raise LookupError(f"位置 {internal_code} 不存在，请先执行位置初始化")
    return location
