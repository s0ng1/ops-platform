"""SSH 主机指纹 TOFU（Trust On First Use）：首次连接记录服务器主机密钥指纹，
后续连接比对，不匹配拒绝（防 MITM）。存储 backend/data/ssh_known_hosts.json，
键 host:port，值 asyncssh SSHKey.get_fingerprint() 的 SHA256 指纹。
文件读写异常静默回退（不阻塞采集主流程）。
"""
import json
import logging

from .config import DATA_DIR

log = logging.getLogger(__name__)

_STORE_PATH = DATA_DIR / "ssh_known_hosts.json"


class SSHHostKeyMismatch(Exception):
    """SSH 主机指纹与首次记录不一致（疑似 MITM）。"""


def _load() -> dict:
    try:
        if _STORE_PATH.exists():
            return json.loads(_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 读失败按空处理
        log.warning("SSH 主机指纹库读取失败，按空处理", exc_info=True)
    return {}


def _save(data: dict) -> None:
    try:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STORE_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - 写失败仅记日志
        log.warning("SSH 主机指纹库写入失败", exc_info=True)


def _key_of(host: str, port: int) -> str:
    return f"{host}:{port}"


def check_or_learn(host: str, port: int, fingerprint: str) -> None:
    """TOFU 核心：指纹未知则记录；一致放行；不一致抛 SSHHostKeyMismatch。"""
    data = _load()
    key = _key_of(host, port)
    existing = data.get(key)
    if existing is None:
        data[key] = fingerprint
        _save(data)
        log.info("已记录 SSH 主机指纹 %s（%s）", key, fingerprint)
        return
    if existing != fingerprint:
        raise SSHHostKeyMismatch(
            f"SSH 主机指纹不匹配 {key}（期望 {existing}，实际 {fingerprint}）"
        )


def verify_connection_host_key(conn, host: str, port: int) -> None:
    """取连接已协商的服务器主机密钥并做 TOFU 记录/比对。
    GSS 密钥交换下 conn.get_server_host_key() 返回 None 时跳过（罕见）。
    """
    key = conn.get_server_host_key()
    if key is None:
        return
    check_or_learn(host, port, key.get_fingerprint())
