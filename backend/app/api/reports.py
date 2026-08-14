"""报表：可用率日报、接口流量日/月报，均支持 format=xlsx 导出 Excel。
双方言策略：PG 且连续聚合 metrics_5m 已建成时走 SQL 聚合（数据量大）；
SQLite（或 cagg 未建成的 PG，静默回退）查原始 metrics 表后用 Python 聚合（测试级数据量）。
"""
import io
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy import bindparam, func, text
from sqlalchemy.orm import Session

from ..core.database import get_db
from ..models import Device, Metric
from .deps import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["报表"])

ONLINE_METRIC = "device_online"  # ping 在线状态：1/0
IN_METRIC, OUT_METRIC = "if_in_bps", "if_out_bps"  # 接口速率，labels={"if": 接口名}

_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _parse_range(start: str, end: str) -> tuple[datetime, datetime, bool]:
    """解析起止时间。end 为纯日期（如 2026-06-10）时含当天，按 < 次日过滤；
    带具体时刻则按 <= 该时刻过滤。返回 (start_dt, end_dt, end_is_exclusive)。
    """
    try:
        start_dt = datetime.fromisoformat(start)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式不正确：{start}")
    try:
        end_dt = datetime.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"时间格式不正确：{end}")
    exclusive = len(end.strip()) <= 10  # 纯日期
    if exclusive:
        end_dt += timedelta(days=1)
    return start_dt, end_dt, exclusive


def _resolve_devices(db: Session, device_id: int | None, device_type: str | None) -> dict[int, Device]:
    """按过滤条件取目标设备集；报表行只覆盖这些设备。"""
    q = db.query(Device)
    if device_id is not None:
        q = q.filter(Device.id == device_id)
    if device_type:
        q = q.filter(Device.type == device_type)
    return {d.id: d for d in q.all()}


def _has_cagg(db: Session) -> bool:
    """PG 且连续聚合 metrics_5m 存在（TimescaleDB 初始化成功）时为真。"""
    if db.get_bind().dialect.name != "postgresql":
        return False
    try:
        return db.execute(text("SELECT to_regclass('metrics_5m')")).scalar() is not None
    except Exception:  # noqa: BLE001 - 查询失败按无 cagg 处理，回退原始表
        return False


def _p95(values: list[float]) -> float | None:
    """95 分位（inclusive 线性插值，与 PG percentile_cont 同口径）。"""
    if not values:
        return None
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=100, method="inclusive")[94])


_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _sanitize_cell(v):
    """Excel 公式注入防护：字符串以 = + - @ 或制表/换行开头时前缀单引号转义为文本。
    operator 可控的设备名/IP 等若以 = 开头会被当作公式执行。"""
    if isinstance(v, str) and v.startswith(_DANGEROUS_PREFIXES):
        return "'" + v
    return v


def _xlsx_response(headers: list[str], rows: list[list], filename: str, sheet: str,
                   percent_cols: tuple[int, ...] = ()) -> StreamingResponse:
    """生成 xlsx 下载响应；percent_cols 为需按百分比显示的 1 基列号。"""
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws.append(headers)
    for r in rows:
        ws.append([_sanitize_cell(v) for v in r])
    for col in percent_cols:
        for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            cell.number_format = "0.00%"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    # RFC 5987：中文文件名走 filename* 百分号编码，filename 保留 ASCII 兜底
    disposition = f"attachment; filename=\"report.xlsx\"; filename*=UTF-8''{quote(filename)}"
    return StreamingResponse(buf, media_type=_XLSX_MEDIA,
                             headers={"Content-Disposition": disposition})


def _check_format(fmt: str | None) -> bool:
    """返回是否导出 xlsx；非法格式值直接 400。"""
    if fmt in (None, "json"):
        return False
    if fmt == "xlsx":
        return True
    raise HTTPException(status_code=400, detail=f"不支持的导出格式：{fmt}")


# ---------- 可用率报表 ----------

def _availability_agg(db: Session, ids: list[int], start_dt: datetime, end_dt: datetime,
                      exclusive: bool) -> list[tuple[int, str, int, float]]:
    """按设备×天聚合 device_online，返回 (device_id, day, total, online_sum)。"""
    if _has_cagg(db):
        # PG：用 5 分钟连续聚合；avg_value 即桶内在线率，sum(avg_value)≈在线点数
        op = "<" if exclusive else "<="
        stmt = text(f"""
            SELECT device_id, date(bucket) AS day, count(*) AS total, sum(avg_value) AS online
            FROM metrics_5m
            WHERE metric = :metric AND bucket >= :start AND bucket {op} :end
              AND device_id IN :ids
            GROUP BY device_id, date(bucket)
        """).bindparams(bindparam("ids", expanding=True))
        rows = db.execute(stmt, {"metric": ONLINE_METRIC, "start": start_dt,
                                 "end": end_dt, "ids": ids}).all()
        return [(r[0], str(r[1])[:10], int(r[2]), float(r[3] or 0)) for r in rows]
    # SQLite / 无 cagg 的 PG：原始表聚合
    day_expr = func.date(Metric.time)
    end_cond = Metric.time < end_dt if exclusive else Metric.time <= end_dt
    q = (db.query(Metric.device_id, day_expr.label("day"),
                  func.count().label("total"), func.sum(Metric.value).label("online"))
         .filter(Metric.metric == ONLINE_METRIC, Metric.device_id.in_(ids),
                 Metric.time >= start_dt, end_cond)
         .group_by(Metric.device_id, day_expr))
    return [(r[0], str(r[1])[:10], int(r[2]), float(r[3] or 0)) for r in q.all()]


@router.get("/availability")
def availability_report(
    start: str = Query(..., description="起始日期/时间，如 2026-06-01"),
    end: str = Query(..., description="结束日期（含当天）或 ISO 时间"),
    device_type: str | None = Query(default=None),
    device_id: int | None = Query(default=None),
    format: str | None = Query(default=None, description="xlsx 导出 Excel"),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """每设备×每日一行；availability = device_online 均值（0~1，前端自行格式化百分比）。"""
    want_xlsx = _check_format(format)
    start_dt, end_dt, exclusive = _parse_range(start, end)
    devices = _resolve_devices(db, device_id, device_type)
    rows = []
    if devices:
        for did, day, total, online in _availability_agg(db, list(devices), start_dt, end_dt, exclusive):
            d = devices.get(did)
            if d is None or total == 0:
                continue
            rows.append({
                "device_id": did, "device_name": d.name, "ip": d.ip, "type": d.type,
                "day": day, "total_points": total, "online_points": round(online, 2),
                "availability": round(online / total, 6),
            })
    rows.sort(key=lambda r: (r["day"], r["device_id"]))
    if want_xlsx:
        data = [[r["device_id"], r["device_name"], r["ip"], r["type"], r["day"],
                 r["total_points"], r["online_points"], r["availability"]] for r in rows]
        return _xlsx_response(
            ["设备ID", "设备名称", "IP", "类型", "日期", "采样点数", "在线点数", "可用率"],
            data, f"可用率报表_{start[:10]}_{end[:10]}.xlsx", "可用率", percent_cols=(8,))
    return {"rows": rows}


# ---------- 接口流量报表 ----------

def _traffic_agg_cagg(db: Session, ids: list[int], start_dt: datetime, end_dt: datetime,
                      exclusive: bool, granularity: str) -> list[dict]:
    """PG cagg 路径：5 分钟桶上 SQL 聚合。p95 为桶均值（含 in/out 两侧）的 95 分位近似。"""
    pexpr = "date(bucket)" if granularity == "day" else "to_char(date_trunc('month', bucket), 'YYYY-MM')"
    op = "<" if exclusive else "<="
    stmt = text(f"""
        SELECT device_id, labels->>'if' AS iface, {pexpr} AS period,
               avg(avg_value) FILTER (WHERE metric = :in_m) AS in_avg,
               max(max_value) FILTER (WHERE metric = :in_m) AS in_max,
               avg(avg_value) FILTER (WHERE metric = :out_m) AS out_avg,
               max(max_value) FILTER (WHERE metric = :out_m) AS out_max,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY avg_value) AS p95
        FROM metrics_5m
        WHERE metric IN (:in_m, :out_m) AND bucket >= :start AND bucket {op} :end
          AND device_id IN :ids
        GROUP BY device_id, labels->>'if', {pexpr}
    """).bindparams(bindparam("ids", expanding=True))
    rows = db.execute(stmt, {"in_m": IN_METRIC, "out_m": OUT_METRIC, "start": start_dt,
                             "end": end_dt, "ids": ids}).all()
    plen = 10 if granularity == "day" else 7
    return [
        {"device_id": r[0], "interface": r[1] or "", "period": str(r[2])[:plen],
         "in_avg": r[3], "in_max": r[4], "out_avg": r[5], "out_max": r[6], "p95": r[7]}
        for r in rows
    ]


def _traffic_agg_raw(db: Session, ids: list[int], start_dt: datetime, end_dt: datetime,
                     exclusive: bool, granularity: str) -> list[dict]:
    """SQLite / 无 cagg 回退路径：取原始点在 Python 侧聚合。"""
    iface_expr = Metric.labels["if"].as_string()
    if db.get_bind().dialect.name == "postgresql":
        period_expr = (func.date(Metric.time) if granularity == "day"
                       else func.to_char(func.date_trunc("month", Metric.time), "YYYY-MM"))
    else:
        period_expr = (func.date(Metric.time) if granularity == "day"
                       else func.strftime("%Y-%m", Metric.time))
    end_cond = Metric.time < end_dt if exclusive else Metric.time <= end_dt
    q = (db.query(Metric.device_id, iface_expr.label("iface"), period_expr.label("period"),
                  Metric.metric, Metric.value)
         .filter(Metric.metric.in_((IN_METRIC, OUT_METRIC)), Metric.device_id.in_(ids),
                 Metric.time >= start_dt, end_cond))
    plen = 10 if granularity == "day" else 7
    groups: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: {"in": [], "out": []})
    for did, iface, period, metric, value in q.all():
        side = "in" if metric == IN_METRIC else "out"
        groups[(did, iface or "", str(period)[:plen])][side].append(value)
    result = []
    for (did, iface, period), vals in groups.items():
        ins, outs = vals["in"], vals["out"]
        result.append({
            "device_id": did, "interface": iface, "period": period,
            "in_avg": statistics.fmean(ins) if ins else None,
            "in_max": max(ins) if ins else None,
            "out_avg": statistics.fmean(outs) if outs else None,
            "out_max": max(outs) if outs else None,
            # p95：周期内 in/out 两侧全部采样值的 95 分位
            "p95": _p95(ins + outs),
        })
    return result


@router.get("/traffic")
def traffic_report(
    start: str = Query(..., description="起始日期/时间，如 2026-06-01"),
    end: str = Query(..., description="结束日期（含当天）或 ISO 时间"),
    granularity: str = Query(default="day", description="day|month"),
    device_id: int | None = Query(default=None),
    top: int | None = Query(default=None, gt=0, description="只保留均值最高的前 N 行"),
    format: str | None = Query(default=None, description="xlsx 导出 Excel"),
    db: Session = Depends(get_db),
    _: object = Depends(get_current_user),
):
    """每设备×每接口×每周期一行，单位 bps。
    p95 = 周期内 in/out 两侧全部采样值的 95 分位（衡量整体忙时水位，非单方向）。
    """
    if granularity not in ("day", "month"):
        raise HTTPException(status_code=400, detail="granularity 仅支持 day|month")
    want_xlsx = _check_format(format)
    start_dt, end_dt, exclusive = _parse_range(start, end)
    devices = _resolve_devices(db, device_id, None)
    agg = []
    if devices:
        ids = list(devices)
        if _has_cagg(db):
            agg = _traffic_agg_cagg(db, ids, start_dt, end_dt, exclusive, granularity)
        else:
            agg = _traffic_agg_raw(db, ids, start_dt, end_dt, exclusive, granularity)
    rows = []
    for a in agg:
        d = devices.get(a["device_id"])
        if d is None:
            continue
        def r2(v):
            return round(float(v), 2) if v is not None else None
        rows.append({
            "device_id": a["device_id"], "device_name": d.name, "ip": d.ip,
            "interface": a["interface"], "period": a["period"],
            "in_avg": r2(a["in_avg"]), "in_max": r2(a["in_max"]),
            "out_avg": r2(a["out_avg"]), "out_max": r2(a["out_max"]), "p95": r2(a["p95"]),
        })
    if top is not None:
        # top 按行级双向均值（in_avg+out_avg）/2 降序截断
        rows.sort(key=lambda r: -((r["in_avg"] or 0) + (r["out_avg"] or 0)))
        rows = rows[:top]
    rows.sort(key=lambda r: (r["period"], r["device_id"], r["interface"]))
    if want_xlsx:
        data = [[r["device_id"], r["device_name"], r["ip"], r["interface"], r["period"],
                 r["in_avg"], r["in_max"], r["out_avg"], r["out_max"], r["p95"]] for r in rows]
        return _xlsx_response(
            ["设备ID", "设备名称", "IP", "接口", "周期", "入向均值(bps)", "入向峰值(bps)",
             "出向均值(bps)", "出向峰值(bps)", "P95(bps)"],
            data, f"流量报表_{granularity}_{start[:10]}_{end[:10]}.xlsx", "接口流量")
    return {"rows": rows}
