"""轻量 IPAM API：终端台账清单（过滤+分页）、白名单/备注维护、子网维度汇总。"""
import ipaddress
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..core import audit
from ..core.database import get_db
from ..models import Device, IpInventory
from .deps import get_current_user, require_operator

router = APIRouter(prefix="/api/ipam", tags=["IPAM"])

ONLINE_SECONDS = 600              # 近 10 分钟见过 = 在线
ACTIVE7D_SECONDS = 7 * 86400      # 近 7 天活跃
MAX_SUBNETS = 16                  # 子网汇总单接口上限，防止大库存一次拉爆响应


class IpInventoryUpdate(BaseModel):
    """可维护字段仅限白名单与 hostname（备注性质），其余字段只由采集写入。"""

    whitelisted: bool | None = None
    hostname: str | None = Field(default=None, max_length=128)


def _device_names(db: Session, rows: list[IpInventory]) -> dict[int, str]:
    ids = {r.device_id for r in rows if r.device_id is not None}
    if not ids:
        return {}
    return {
        d.id: d.name for d in db.query(Device).filter(Device.id.in_(ids)).all()
    }


def _row_out(row: IpInventory, device_names: dict[int, str]) -> dict:
    return {
        "id": row.id,
        "ip": row.ip,
        "mac": row.mac,
        "hostname": row.hostname,
        "device_id": row.device_id,
        "device_name": device_names.get(row.device_id) if row.device_id else None,
        "if_name": row.if_name,
        "source": row.source,
        "whitelisted": row.whitelisted,
        "first_seen": row.first_seen,
        "last_seen": row.last_seen,
    }


def _subnet_prefix(text: str) -> str:
    """子网过滤统一成点分前缀做字符串匹配（SQLite/PG 双方言安全）：
    "203.0.113.0/24" → "203.0.113."；不带前缀长度的写法按原文本前缀匹配。"""
    text = text.strip()
    if "/" in text:
        try:
            net = ipaddress.ip_network(text, strict=False)
            if net.version == 4 and net.prefixlen % 8 == 0 and net.prefixlen > 0:
                head = str(net.network_address).split(".")[: net.prefixlen // 8]
                return ".".join(head) + "."
        except ValueError:
            pass
    return text


def _ip_status(last_seen: datetime | None, now: datetime) -> str:
    """online=近 10 分钟 / active7d=近 7 天 / stale=更早。"""
    if last_seen is None:
        return "stale"
    age = (now - last_seen).total_seconds()
    if age <= ONLINE_SECONDS:
        return "online"
    if age <= ACTIVE7D_SECONDS:
        return "active7d"
    return "stale"


def _ip_sort_key(ip: str):
    try:
        return (0, int(ipaddress.IPv4Address(ip)))
    except ValueError:
        return (1, ip)


# ---- 终端清单 ----


@router.get("/inventory")
def list_inventory(
    keyword: str = Query(default="", description="IP 关键字（包含匹配）"),
    mac: str = Query(default="", description="MAC 关键字（包含匹配）"),
    subnet: str = Query(default="", description="子网前缀，如 203.0.113 或 203.0.113.0/24"),
    source: str = Query(default=""),
    whitelisted: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    q = db.query(IpInventory)
    if keyword:
        q = q.filter(IpInventory.ip.contains(keyword))
    if mac:
        q = q.filter(IpInventory.mac.contains(mac.strip().lower()))
    if subnet:
        q = q.filter(IpInventory.ip.startswith(_subnet_prefix(subnet)))
    if source:
        q = q.filter(IpInventory.source == source)
    if whitelisted is not None:
        q = q.filter(IpInventory.whitelisted.is_(whitelisted))
    total = q.count()
    rows = (
        q.order_by(IpInventory.last_seen.desc(), IpInventory.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    names = _device_names(db, rows)
    return {"total": total, "items": [_row_out(r, names) for r in rows]}


@router.put("/inventory/{row_id}")
def update_inventory(
    row_id: int,
    body: IpInventoryUpdate,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(require_operator),
):
    """维护白名单 / hostname 备注；其余字段不接受修改。"""
    row = db.get(IpInventory, row_id)
    if row is None:
        raise HTTPException(status_code=404, detail="台账记录不存在")
    changes = []
    if body.whitelisted is not None and body.whitelisted != row.whitelisted:
        row.whitelisted = body.whitelisted
        changes.append(f"whitelisted={body.whitelisted}")
    if body.hostname is not None:
        row.hostname = body.hostname or None  # 空串=清空备注
        changes.append("hostname")
    db.commit()
    if changes:
        audit.record(user.username, "ipam_update", target=row.ip,
                     detail=" ".join(changes), ip=audit.client_ip(request))
    return _row_out(row, _device_names(db, [row]))


# ---- 子网维度汇总 ----


@router.get("/subnets")
def subnet_summary(
    prefix_len: int = Query(default=24, ge=8, le=30),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """按子网（默认 /24）聚合一页网格数据：每子网 total/online/active7d 计数 +
    库存内每个 IP 的状态数组（库存没有的 IP 不返回，前端自行补「未见」态）。
    子网数超过 MAX_SUBNETS 时按地址序截断。"""
    rows = db.query(IpInventory).all()
    names = _device_names(db, rows)
    now = datetime.now()
    groups: dict[ipaddress.IPv4Network, list[IpInventory]] = {}
    for r in rows:
        try:
            net = ipaddress.ip_network(f"{r.ip}/{prefix_len}", strict=False)
        except ValueError:
            continue
        groups.setdefault(net, []).append(r)

    subnets = []
    for net in sorted(groups, key=lambda n: int(n.network_address))[:MAX_SUBNETS]:
        members = sorted(groups[net], key=lambda r: _ip_sort_key(r.ip))
        ips = []
        online = active7d = 0
        for m in members:
            status = _ip_status(m.last_seen, now)
            if status == "online":
                online += 1
            if status in ("online", "active7d"):
                active7d += 1
            ips.append(
                {
                    "ip": m.ip,
                    "mac": m.mac,
                    "hostname": m.hostname,
                    "device_name": names.get(m.device_id) if m.device_id else None,
                    "if_name": m.if_name,
                    "source": m.source,
                    "whitelisted": m.whitelisted,
                    "last_seen": m.last_seen,
                    "status": status,
                }
            )
        subnets.append(
            {
                "subnet": str(net),
                "total": len(members),
                "online": online,
                "active7d": active7d,
                "ips": ips,
            }
        )
    return {"prefix_len": prefix_len, "total_subnets": len(groups), "subnets": subnets}
