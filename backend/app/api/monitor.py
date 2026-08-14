"""监控总览：仪表盘计数。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models import Device
from .deps import get_current_user
from .schemas import OverviewOut

router = APIRouter(prefix="/api/monitor", tags=["监控"])


@router.get("/overview", response_model=OverviewOut)
def overview(
    db: Session = Depends(get_db), _: object = Depends(get_current_user)
):
    rows = db.query(Device.status, func.count()).group_by(Device.status).all()
    counts = {status: n for status, n in rows}
    type_rows = db.query(Device.type, func.count()).group_by(Device.type).all()
    return OverviewOut(
        total=sum(counts.values()),
        online=counts.get("online", 0),
        offline=counts.get("offline", 0),
        unknown=counts.get("unknown", 0),
        by_type={t: n for t, n in type_rows},
    )
