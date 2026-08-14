"""登录限流：进程内存滑动窗口（单进程部署；多实例需 Redis/共享存储，见核心决策）。
无第三方依赖。按 (ip, username) 与纯 ip 两个维度计数：
连续失败超阈值则锁定一段时间，登录成功清空该用户名维度的计数（ip 维度保留，防同 IP 换用户名爆破）。
"""
import time
from collections import defaultdict, deque

FAIL_WINDOW = 900        # 计数窗口（秒）
USER_MAX_FAILS = 5       # 同 (ip, username) 连续失败上限
IP_MAX_FAILS = 20        # 同 ip 连续失败上限
LOCK_SECONDS = 900       # 锁定时长（秒）

_fail_log: dict[tuple, deque] = defaultdict(deque)
_locks: dict[tuple, float] = {}  # key -> 解锁时刻（monotonic）


def _now() -> float:
    return time.monotonic()


def _prune(key: tuple) -> None:
    dq = _fail_log[key]
    cutoff = _now() - FAIL_WINDOW
    while dq and dq[0] < cutoff:
        dq.popleft()


def check_login_allowed(ip: str, username: str) -> float | None:
    """超限返回还需等待的秒数，否则返回 None 放行。"""
    now = _now()
    for key in ((ip, username), (ip,)):
        until = _locks.get(key, 0)
        if until > now:
            return until - now
    return None


def note_failure(ip: str, username: str) -> None:
    """记录一次登录失败；达到阈值则锁定对应维度。"""
    now = _now()
    for key in ((ip, username), (ip,)):
        _prune(key)
        dq = _fail_log[key]
        dq.append(now)
        limit = USER_MAX_FAILS if len(key) == 2 else IP_MAX_FAILS
        if len(dq) >= limit:
            _locks[key] = now + LOCK_SECONDS


def note_success(ip: str, username: str) -> None:
    """登录成功：清空该 (ip, username) 的失败计数与锁定（ip 维度保留）。"""
    key = (ip, username)
    _fail_log.pop(key, None)
    _locks.pop(key, None)
