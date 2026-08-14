"""应用配置：环境变量优先，缺省值为本地开发用。"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent.parent  # backend/
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPS_", env_file=".env", extra="ignore")

    app_name: str = "内网运维管理平台"
    # 开发默认 SQLite；生产设 OPS_DATABASE_URL=postgresql+psycopg://user:pass@host/ops
    database_url: str = f"sqlite:///{DATA_DIR}/ops_platform.db"
    # JWT 签名密钥，生产必须改（HS256 要求 ≥32 字节）
    secret_key: str = "dev-only-secret-change-me-0123456789abcdef"
    token_expire_hours: int = 12
    # 凭据 Fernet 密钥；为空时自动生成并持久化到 data/fernet.key
    fernet_key: str = ""
    # 监控轮询周期（秒）
    monitor_interval: int = 60
    # 数据库连接池（仅 PostgreSQL 生效，SQLite 忽略）；并发采集/告警评估会短时占满连接
    db_pool_size: int = 20
    db_max_overflow: int = 40
    # 首次启动创建的默认管理员
    admin_username: str = "admin"
    admin_password: str = "admin123"
    # Syslog / SNMP Trap 接收器（UDP，避开特权端口 514/162）
    log_receiver_enabled: bool = True
    syslog_port: int = 1514
    trap_port: int = 1162
    # 来源 IP 白名单（逗号分隔 IP/CIDR；空=全部接收）。内网任意主机可灌假日志/告警，建议生产配置
    syslog_allow: str = ""
    trap_allow: str = ""
    # 应用拨测 SSRF 防护是否封回环（生产默认封；测试起本地假服务用 127.0.0.1，故 conftest 关闭）
    ssrf_block_loopback: bool = True


@lru_cache
def get_settings() -> Settings:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return Settings()
