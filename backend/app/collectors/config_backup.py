"""网络设备配置文件备份采集（SSH）：按厂商命令表取配置文本，sha256 去重入库。
变更检测通过 config_changed 指标点交给告警引擎（内置「配置变更」规则），
采集器本身不直接碰告警体系。全部失败静默回退，不阻塞调度主流程。
"""
import asyncio
import hashlib
import logging
from datetime import datetime, timezone

from ..core.database import SessionLocal
from ..core.ssh_hostkeys import verify_connection_host_key
from ..models import ConfigBackup, Device
from .snmp_metrics import MetricPoint

log = logging.getLogger(__name__)

SSH_CONNECT_TIMEOUT = 5
SSH_CMD_TIMEOUT = 60  # 框式设备 display current-configuration 输出大、回显慢

# 单版本内容上限，超出截断保护（避免异常设备刷爆存储）
MAX_CONFIG_BYTES = 1024 * 1024

# 厂商命令表：sysObjectID 前缀 -> 取配置命令（与 snmp_metrics.VENDOR_METRICS 同套指纹）
VENDOR_COMMANDS = {
    "1.3.6.1.4.1.9": "show running-config",               # Cisco
    "1.3.6.1.4.1.2011": "display current-configuration",  # 华为
    "1.3.6.1.4.1.25506": "display current-configuration", # H3C
}
DEFAULT_COMMAND = "display current-configuration"  # 未识别厂商按 H3C 命令兜底


def select_command(device: Device) -> str:
    """按设备 sysObjectID 指纹选厂商命令，未识别用兜底命令。"""
    sys_oid = device.sys_object_id or ""
    prefix = next((p for p in VENDOR_COMMANDS if sys_oid.startswith(p)), None)
    return VENDOR_COMMANDS.get(prefix, DEFAULT_COMMAND)


def _truncate(text: str) -> str:
    """按字节截断到上限，截断处可能切断多字节字符，忽略解码错误。"""
    raw = text.encode("utf-8")
    if len(raw) <= MAX_CONFIG_BYTES:
        return text
    return raw[:MAX_CONFIG_BYTES].decode("utf-8", errors="ignore")


def config_changed_point(device_id: int, changed: bool) -> MetricPoint:
    """构造 config_changed 指标点（1=本轮检测到变更），供告警引擎评估。"""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return MetricPoint(device_id, "config_changed", 1.0 if changed else 0.0, {}, now)


async def _run_config_commands(host: str, payload: dict, commands: list[str]) -> list[str]:
    """建立一次 SSH 连接，顺序执行命令，返回 stdout 列表。失败抛异常由调用方隔离。"""
    import asyncssh

    port = int(payload.get("port", 22))
    async with asyncio.timeout(SSH_CONNECT_TIMEOUT + SSH_CMD_TIMEOUT * len(commands)):
        async with asyncssh.connect(
            host,
            port=port,
            username=payload.get("username", ""),
            password=payload.get("password", ""),
            known_hosts=None,  # 改由 verify_connection_host_key 做 TOFU 指纹校验
            connect_timeout=SSH_CONNECT_TIMEOUT,
        ) as conn:
            verify_connection_host_key(conn, host, port)
            results = []
            for cmd in commands:
                r = await conn.run(cmd, timeout=SSH_CMD_TIMEOUT)
                results.append(r.stdout or "")
            return results


def _store_if_changed(device_id: int, digest: str, content: str) -> tuple[str, int] | None:
    """与最新版本比对 hash，不同才入库新版本。同步 DB 操作，供 to_thread 调用。
    返回 (状态, backup_id)：baseline=首个版本 / changed=有变更 / same=无变化；None=DB 失败。
    """
    db = SessionLocal()
    try:
        last = (
            db.query(ConfigBackup)
            .filter(ConfigBackup.device_id == device_id)
            .order_by(ConfigBackup.id.desc())
            .first()
        )
        if last is not None and last.content_hash == digest:
            return "same", last.id
        backup = ConfigBackup(device_id=device_id, content_hash=digest, content=content)
        db.add(backup)
        db.commit()
        return ("changed" if last is not None else "baseline"), backup.id
    except Exception:  # noqa: BLE001
        db.rollback()
        log.exception("配置备份入库失败 device_id=%s", device_id)
        return None
    finally:
        db.close()


async def fetch_config(
    device: Device,
    payload: dict,
    run_commands=_run_config_commands,
) -> dict:
    """拉取一台设备配置并按 hash 去重入库。run_commands 可注入便于测试。
    返回 {"status": baseline|changed|same|failed, ...}，失败静默不抛出。
    """
    host = device.ip
    cmd = select_command(device)
    try:
        outputs = await run_commands(host, payload, [cmd])
    except Exception as e:  # noqa: BLE001 - SSH 失败本轮整体跳过
        log.debug("配置备份采集失败 %s: %s", host, e)
        return {"status": "failed"}
    content = _truncate(outputs[0] if outputs else "")
    if not content.strip():
        log.debug("配置备份内容为空 %s", host)
        return {"status": "failed"}
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    info = await asyncio.to_thread(_store_if_changed, device.id, digest, content)
    if info is None:
        return {"status": "failed"}
    status, backup_id = info
    return {"status": status, "backup_id": backup_id, "content_hash": digest}


async def collect_config_backup(
    device: Device,
    payload: dict,
    run_commands=_run_config_commands,
) -> list[MetricPoint]:
    """调度器入口：拉配置入库，返回 config_changed 点供告警引擎评估（变更才为 1）。"""
    result = await fetch_config(device, payload, run_commands)
    if result["status"] == "failed":
        return []
    return [config_changed_point(device.id, result["status"] == "changed")]
