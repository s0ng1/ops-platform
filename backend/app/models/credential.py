"""凭据模型：SNMP v2c/v3、SSH、数据库账号，payload JSON 整体 Fernet 加密存储。"""
import json
from datetime import datetime

from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from ..core.crypto import decrypt_text, encrypt_text
from ..core.database import Base

CREDENTIAL_KINDS = ("snmp_v2c", "snmp_v3", "ssh", "database")


class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def set_payload(self, data: dict) -> None:
        self.payload_encrypted = encrypt_text(json.dumps(data, ensure_ascii=False))

    def get_payload(self) -> dict:
        try:
            return json.loads(decrypt_text(self.payload_encrypted))
        except Exception:  # noqa: BLE001 - 解密失败静默回退为空
            return {}
