"""动态基线：同设备同指标近 7 天同时段（当前时刻 ±30min，每天一段，共 7 段）的均值/标准差。

双方言说明（为什么不走 cagg metrics_5m）：
- cagg 只有 avg/max/min，没有 stddev 也没有 count——从桶均值既算不出标准差，
  也无法加权还原整体均值；给 cagg 加列需 DROP 重建物化视图并刷新历史，迁移成本高。
- 故 PG/SQLite 统一查原始 metrics 表（7 天窗口远在 30 天保留期内），
  走 (device_id, metric, time) 索引，7 段 1 小时窗口数据量很小；
  引擎侧按 (设备, 指标) 去重后每批只查一次并内存缓存，不会逐点查库。
- PG 用 SQL 聚合（avg/stddev_samp/count）一次出结果；SQLite 无 stddev 聚合函数，
  取回样本值用 Python statistics 聚合（照 api/reports.py 的双方言回退模式）。
"""
import logging
import statistics
from datetime import datetime, timedelta

from sqlalchemy import func, or_

from ..core.database import SessionLocal
from ..models import Metric

log = logging.getLogger(__name__)

WINDOW_DAYS = 7      # 回看天数（每天一段同时段窗口）
WINDOW_MINUTES = 30  # 同时段半窗宽：当前时刻 ±30min
MIN_SAMPLES = 10     # 有效样本下限，不足不触发（新设备/新指标不乱报）


def _windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """近 7 天每天一段 [同时刻-30min, 同时刻+30min] 窗口（与 metrics.time 同为 UTC 朴素时间）。"""
    return [
        (now - timedelta(days=d, minutes=WINDOW_MINUTES),
         now - timedelta(days=d) + timedelta(minutes=WINDOW_MINUTES))
        for d in range(1, WINDOW_DAYS + 1)
    ]


def _query_one(db, device_id: int, metric: str, windows: list) -> tuple[float, float, int]:
    """查单对 (设备, 指标) 的基线，返回 (均值, 标准差, 样本数)。
    基线不拆 labels（按设备+指标全体样本计算）——多序列指标（如 if_* 每口一条线）
    不适合 baseline_dev，基线类规则应配在设备级单值指标上（CPU/内存/时延等）。
    """
    cond = or_(*[(Metric.time >= s) & (Metric.time <= e) for s, e in windows])
    base = (Metric.device_id == device_id) & (Metric.metric == metric) & cond
    if db.get_bind().dialect.name == "postgresql":
        avg, std, cnt = db.query(
            func.avg(Metric.value), func.stddev_samp(Metric.value), func.count()
        ).filter(base).one()
        if not cnt:
            return (0.0, 0.0, 0)
        # stddev_samp 在样本 <2 时返回 NULL，按 0 处理
        return (float(avg), float(std or 0.0), int(cnt))
    values = [v for (v,) in db.query(Metric.value).filter(base).all()]
    if not values:
        return (0.0, 0.0, 0)
    avg = statistics.fmean(values)
    std = statistics.stdev(values) if len(values) > 1 else 0.0
    return (avg, std, len(values))


def load_baselines(pairs: set[tuple[int, str]], now: datetime) -> dict:
    """批量查询 {(device_id, metric)} 的基线 {(device_id, metric): (均值, 标准差, 样本数)}。
    单对查询异常静默跳过（结果里缺该 key = 无基线，评估时不触发），不阻塞整批评估。
    """
    result: dict = {}
    if not pairs:
        return result
    windows = _windows(now)
    db = SessionLocal()
    try:
        for device_id, metric in pairs:
            try:
                result[(device_id, metric)] = _query_one(db, device_id, metric, windows)
            except Exception:  # noqa: BLE001
                db.rollback()
                log.warning("基线查询失败 device=%s metric=%s，本次跳过", device_id, metric,
                            exc_info=True)
    finally:
        db.close()
    return result


def is_breach(value: float, n_sigma: float, baseline: tuple[float, float, int]) -> bool:
    """偏离判定：样本不足不触发；|值-均值| > N×标准差 触发（上下偏离都告）。
    标准差为 0 时退化为「与均值不等即偏离」（任何非零偏差都超 0σ），语义自然成立。
    """
    avg, std, cnt = baseline
    if cnt < MIN_SAMPLES:
        return False
    return abs(value - avg) > n_sigma * std
