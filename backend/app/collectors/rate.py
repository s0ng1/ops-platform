"""计数器速率计算：差值÷间隔，处理 32/64 位计数器回绕与设备重启清零。
进程内缓存上次采样值，重启后首周期无速率（返回 None），属预期行为。
"""


class RateCalculator:
    def __init__(self) -> None:
        self._last: dict[tuple, tuple[float, int]] = {}  # key -> (ts_epoch, raw)

    def rate(self, key: tuple, ts: float, raw: int, bits: int = 64) -> float | None:
        """返回每秒速率；首见/异常时返回 None。bits 为计数器位宽（SNMP 计数器通常 64）。"""
        prev = self._last.get(key)
        self._last[key] = (ts, raw)
        if prev is None:
            return None
        prev_ts, prev_raw = prev
        interval = ts - prev_ts
        if interval <= 0:
            return None
        if raw >= prev_raw:
            delta = raw - prev_raw
        else:
            # 计数器回绕（按位宽补）或设备重启清零（差值过大则丢弃本周期）
            wrapped = (1 << bits) - prev_raw + raw
            if wrapped > (1 << bits) // 2:  # 更可能是重启清零而非回绕
                return None
            delta = wrapped
        return delta / interval
