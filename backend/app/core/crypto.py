"""凭据对称加密（Fernet）。密钥优先取环境变量 OPS_FERNET_KEY，
缺省时自动生成并保存到 data/fernet.key（仅限开发，生产必须显式配置）。"""
from cryptography.fernet import Fernet

from .config import DATA_DIR, get_settings

_KEY_FILE = DATA_DIR / "fernet.key"


def _load_key() -> bytes:
    key = get_settings().fernet_key
    if key:
        return key.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key_bytes = Fernet.generate_key()
    _KEY_FILE.write_bytes(key_bytes)
    _KEY_FILE.chmod(0o600)
    return key_bytes


_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt_text(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt_text(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
