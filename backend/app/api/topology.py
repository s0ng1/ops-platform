"""拓扑 API：图数据（节点+链路+实时流量）、连线 CRUD、布局保存、LLDP/CDP 自动发现。"""
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core.database import SessionLocal, get_db
from ..models import Device, Metric, TopoLink, TopologyLayout
from ..topology.discovery import discover_device_neighbors, resolve_device_id
from .deps import get_current_user, require_operator

router = APIRouter(prefix="/api/topology", tags=["拓扑"])
log = logging.getLogger(__name__)

TRAFFIC_METRICS = ("if_in_bps", "if_out_bps", "if_in_util", "if_out_util")

# 网络拓扑只放网络/安全设备（主机/数据库属应用层，不进拓扑图）
TOPO_DEVICE_TYPES = ("network", "security")


class LinkIn(BaseModel):
    src_device_id: int
    src_port: str = Field(default="", max_length=64)
    dst_device_id: int
    dst_port: str = Field(default="", max_length=64)


class LinkOut(LinkIn):
    id: int
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}


class LayoutIn(BaseModel):
    positions: list[dict]  # [{"device_id": 1, "x": 100.0, "y": 200.0}]
    # 分组名：空=全图布局（写 devices.pos_x/pos_y），非空=该分组的独立布局（写 topology_layouts）
    group: str = Field(default="", max_length=128)


def _link_key(src_id: int, src_port: str, dst_id: int, dst_port: str) -> tuple:
    """方向无关的链路去重键。"""
    a, b = (src_id, src_port or ""), (dst_id, dst_port or "")
    return (a, b) if a <= b else (b, a)


def _latest_traffic(db: Session, device_ids: set[int]) -> dict:
    """取相关设备接口流量指标的最新值：{(device_id, metric, if名): value}。
    必须加时间窗：hypertable 全表 GROUP BY labels 在大接口数设备（551 口核心）上要 1~3.5s，
    最新值必落在最近几个采集周期内，10 分钟窗口足够且走 (device_id,time) 索引毫秒级。"""
    if not device_ids:
        return {}
    since = datetime.utcnow() - timedelta(minutes=10)
    sub = (
        db.query(Metric.device_id, Metric.metric, Metric.labels, func.max(Metric.time).label("mt"))
        .filter(Metric.device_id.in_(device_ids), Metric.metric.in_(TRAFFIC_METRICS),
                Metric.time > since)
        .group_by(Metric.device_id, Metric.metric, Metric.labels)
        .subquery()
    )
    rows = (
        db.query(Metric)
        .join(
            sub,
            (Metric.device_id == sub.c.device_id)
            & (Metric.metric == sub.c.metric)
            & (Metric.labels == sub.c.labels)
            & (Metric.time == sub.c.mt),
        )
        .all()
    )
    result = {}
    for r in rows:
        if_name = (r.labels or {}).get("if", "")
        result[(r.device_id, r.metric, if_name)] = r.value
    return result


def _port_traffic(traffic: dict, device_id: int, port: str) -> dict:
    def g(metric: str):
        v = traffic.get((device_id, metric, port))
        return round(v, 2) if v is not None else None

    return {
        "in_bps": g("if_in_bps"),
        "out_bps": g("if_out_bps"),
        "in_util": g("if_in_util"),
        "out_util": g("if_out_util"),
    }


@router.get("/groups")
def list_groups(db: Session = Depends(get_db), _: object = Depends(get_current_user)):
    """现有分组清单（仅 network/security 设备参与拓扑分组）：组名 + 组内设备数。"""
    rows = (
        db.query(Device.group_name, func.count(Device.id))
        .filter(Device.type.in_(TOPO_DEVICE_TYPES), Device.group_name != "")
        .group_by(Device.group_name)
        .order_by(Device.group_name)
        .all()
    )
    return [{"name": name, "count": count} for name, count in rows]


@router.get("/graph")
def get_graph(
    group: str = Query(default="", max_length=128),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    q = db.query(Device).filter(Device.type.in_(TOPO_DEVICE_TYPES))
    if group:
        # 分组子拓扑：只出该组节点，跨界链路数据保留但不返回（照全图同一口径）
        q = q.filter(Device.group_name == group)
    devices = q.order_by(Device.id).all()
    node_ids = {d.id for d in devices}
    # 分组布局覆盖：topology_layouts 有记录优先，没有回退 devices.pos_x/pos_y（全图布局）
    group_pos = {}
    if group and node_ids:
        group_pos = {
            r.device_id: (r.pos_x, r.pos_y)
            for r in db.query(TopologyLayout)
            .filter(TopologyLayout.group_name == group,
                    TopologyLayout.device_id.in_(node_ids))
            .all()
        }
    # 链路只保留两端都在图内的
    links = [
        l for l in db.query(TopoLink).all()
        if l.src_device_id in node_ids and l.dst_device_id in node_ids
    ]
    device_ids = {l.src_device_id for l in links} | {l.dst_device_id for l in links}
    traffic = _latest_traffic(db, device_ids)
    def _node_xy(d: Device) -> tuple:
        # 分组视图下有分组布局记录优先用，没有则回退全图坐标
        if group and d.id in group_pos:
            return group_pos[d.id]
        return d.pos_x, d.pos_y

    return {
        "nodes": [
            {
                "id": d.id, "name": d.name, "ip": d.ip, "type": d.type,
                "subtype": d.subtype,
                "status": d.status,
                "x": _node_xy(d)[0],
                "y": _node_xy(d)[1],
            }
            for d in devices
        ],
        "links": [
            {
                "id": l.id,
                "src_device_id": l.src_device_id, "src_port": l.src_port,
                "dst_device_id": l.dst_device_id, "dst_port": l.dst_port,
                "source": l.source,
                "src_traffic": _port_traffic(traffic, l.src_device_id, l.src_port),
                "dst_traffic": _port_traffic(traffic, l.dst_device_id, l.dst_port),
            }
            for l in links
        ],
    }


@router.get("/traffic")
def get_link_traffic(
    group: str = Query(default="", max_length=128),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """链路实时流量（轻量版 graph，供前端 5s 轮询）：只返回链路两端接口最新流量。"""
    q = db.query(Device.id).filter(Device.type.in_(TOPO_DEVICE_TYPES))
    if group:
        q = q.filter(Device.group_name == group)
    node_ids = {r.id for r in q.all()}
    links = [
        l for l in db.query(TopoLink).all()
        if l.src_device_id in node_ids and l.dst_device_id in node_ids
    ]
    device_ids = {l.src_device_id for l in links} | {l.dst_device_id for l in links}
    traffic = _latest_traffic(db, device_ids)
    return {
        "links": [
            {
                "id": l.id,
                "src_traffic": _port_traffic(traffic, l.src_device_id, l.src_port),
                "dst_traffic": _port_traffic(traffic, l.dst_device_id, l.dst_port),
            }
            for l in links
        ],
    }


@router.post("/links", response_model=LinkOut, status_code=201)
def create_link(
    body: LinkIn, db: Session = Depends(get_db), _: object = Depends(require_operator)
):
    if body.src_device_id == body.dst_device_id and not (body.src_port or body.dst_port):
        raise HTTPException(status_code=400, detail="不允许无端口的自环链路")
    for did in (body.src_device_id, body.dst_device_id):
        if db.get(Device, did) is None:
            raise HTTPException(status_code=400, detail=f"设备 {did} 不存在")
    key = _link_key(body.src_device_id, body.src_port, body.dst_device_id, body.dst_port)
    for l in db.query(TopoLink).all():
        if _link_key(l.src_device_id, l.src_port, l.dst_device_id, l.dst_port) == key:
            raise HTTPException(status_code=409, detail="该链路已存在")
    link = TopoLink(source="manual", **body.model_dump())
    db.add(link)
    db.commit()
    db.refresh(link)
    return link


@router.delete("/links/{link_id}")
def delete_link(link_id: int, db: Session = Depends(get_db), _: object = Depends(require_operator)):
    link = db.get(TopoLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="链路不存在")
    db.delete(link)
    db.commit()
    return {"ok": True}


@router.put("/layout")
def save_layout(
    body: LayoutIn, db: Session = Depends(get_db), _: object = Depends(require_operator)
):
    """批量保存节点坐标。group 为空写 devices.pos_x/pos_y（全图布局）；
    非空写 topology_layouts（该分组独立布局，不动全图坐标）。"""
    updated = 0
    for p in body.positions:
        device = db.get(Device, int(p.get("device_id", 0)))
        if device is None:
            continue
        try:
            x, y = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if body.group:
            row = (
                db.query(TopologyLayout)
                .filter(TopologyLayout.device_id == device.id,
                        TopologyLayout.group_name == body.group)
                .first()
            )
            if row is None:
                row = TopologyLayout(device_id=device.id, group_name=body.group)
                db.add(row)
            row.pos_x, row.pos_y = x, y
        else:
            device.pos_x, device.pos_y = x, y
        updated += 1
    db.commit()
    return {"updated": updated}


@router.post("/discover")
async def discover_links(_: object = Depends(require_operator)):
    """对全部带 SNMP 凭据的网络/安全设备跑 LLDP/CDP 邻居发现，自动建链路。
    设备级并发 8（23 台实测串行 >10 分钟、并发后约 1 分钟），返回发现摘要。
    """
    db = SessionLocal()
    try:
        devices = [
            d for d in db.query(Device).all()
            if d.credential and d.credential.kind in ("snmp_v2c", "snmp_v3")
            and d.type in ("network", "security")
        ]
        all_devices = db.query(Device).all()
        existing = {
            _link_key(l.src_device_id, l.src_port, l.dst_device_id, l.dst_port)
            for l in db.query(TopoLink).all()
        }
        summary = {"scanned": len(devices), "neighbors": 0, "created": 0, "skipped": 0, "unmatched": []}

        sem = asyncio.Semaphore(8)

        async def _one(device: Device):
            payload = device.credential.get_payload()
            payload["kind"] = device.credential.kind
            async with sem:
                try:
                    return device, await discover_device_neighbors(device, payload)
                except Exception as e:  # noqa: BLE001 - 单台失败不阻塞
                    log.debug("拓扑发现失败 %s: %s", device.ip, e)
                    return device, []

        for device, neighbors in await asyncio.gather(*(_one(d) for d in devices)):
            for nb in neighbors:
                summary["neighbors"] += 1
                remote_id = resolve_device_id(nb, all_devices)
                if remote_id is None or remote_id == device.id:
                    summary["unmatched"].append(
                        {"device": device.name, "remote_name": nb.remote_name,
                         "remote_ip": nb.remote_ip, "remote_port": nb.remote_port}
                    )
                    continue
                key = _link_key(device.id, nb.local_port, remote_id, nb.remote_port)
                if key in existing:
                    summary["skipped"] += 1
                    continue
                db.add(TopoLink(
                    src_device_id=device.id, src_port=nb.local_port,
                    dst_device_id=remote_id, dst_port=nb.remote_port, source=nb.source,
                ))
                existing.add(key)
                summary["created"] += 1
        db.commit()
        return summary
    finally:
        db.close()
