from app.collectors.rate import RateCalculator


def test_first_sample_no_rate():
    rc = RateCalculator()
    assert rc.rate(("d1", "1", "in"), ts=100.0, raw=1000) is None


def test_normal_rate():
    rc = RateCalculator()
    rc.rate(("d1", "1", "in"), ts=100.0, raw=1000)
    # 60 秒增加 60000 字节 → 1000 B/s
    assert rc.rate(("d1", "1", "in"), ts=160.0, raw=61000) == 1000.0


def test_counter_wrap_64bit():
    rc = RateCalculator()
    max64 = (1 << 64) - 1
    rc.rate(("d1",), ts=0.0, raw=max64 - 10)
    # 回绕后从 5 开始：差值 = 10 + 5 + 1 = 16，10 秒 → 1.6/s
    assert rc.rate(("d1",), ts=10.0, raw=5) == 1.6


def test_counter_reset_returns_none():
    rc = RateCalculator()
    rc.rate(("d1",), ts=0.0, raw=10**12)  # 巨大值
    # 设备重启清零：按回绕算的差值超过位宽一半，判定为清零，丢弃本周期
    assert rc.rate(("d1",), ts=60.0, raw=100) is None
    # 下一周期恢复正常
    assert rc.rate(("d1",), ts=120.0, raw=700) == 10.0


def test_zero_interval_no_rate():
    rc = RateCalculator()
    rc.rate(("d1",), ts=100.0, raw=1)
    assert rc.rate(("d1",), ts=100.0, raw=2) is None
