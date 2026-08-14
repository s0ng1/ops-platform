"""Pydantic 请求/响应模型。"""
from datetime import datetime

from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.ssrf import is_blocked_ip

# ---- 认证 ----


def _check_password_strength(v: str) -> str:
    """密码强度：至少 8 位（Field 约束）且必须同时含字母与数字。"""
    if not (any(c.isalpha() for c in v) and any(c.isdigit() for c in v)):
        raise ValueError("密码必须同时包含字母和数字")
    return v


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    token: str
    username: str
    role: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    disabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreateIn(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=8, max_length=128)
    role: str = Field(pattern="^(admin|operator|viewer)$")

    @field_validator("password")
    @classmethod
    def _password_complex(cls, v: str) -> str:
        return _check_password_strength(v)


class UserUpdateIn(BaseModel):
    """用户编辑：改角色 / 禁用启用，字段可单独提交。"""

    role: str | None = Field(default=None, pattern="^(admin|operator|viewer)$")
    disabled: bool | None = None


class PasswordChangeIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def _new_password_complex(cls, v: str) -> str:
        return _check_password_strength(v)


# ---- 凭据 ----


class CredentialIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    kind: str = Field(pattern="^(snmp_v2c|snmp_v3|ssh|database)$")
    payload: dict = Field(default_factory=dict)


class CredentialOut(BaseModel):
    id: int
    name: str
    kind: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 设备 ----


class DeviceIn(BaseModel):
    name: str = Field(default="", max_length=128)
    # 对 application 类型语义为目标主机（IP 或域名），其余类型必须是 IP
    ip: str = Field(min_length=1, max_length=64)
    type: str = Field(default="other")
    subtype: str = Field(default="", max_length=32)  # 网络设备细分：switch/router/firewall，空=按 type 默认
    group_name: str = Field(default="", max_length=128)
    location: str = Field(default="", max_length=128)
    credential_id: int | None = None
    # 配置备份专用 SSH 凭据（仅 network/security 生效），非空时必须是 ssh 类型（端点校验）
    ssh_credential_id: int | None = None
    monitor_enabled: bool = True
    # 应用拨测配置（仅 type=application 生效）
    probe_config: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_probe_config(self):
        if self.type != "application":
            return self
        cfg = self.probe_config or {}
        kind = cfg.get("probe_kind")
        if kind not in ("http", "dns", "tcp", "nginx", "redis"):
            raise ValueError("拨测类型 probe_kind 必须是 http/dns/tcp/nginx/redis")
        timeout = cfg.get("timeout")
        if timeout is not None and not (1 <= float(timeout) <= 60):
            raise ValueError("超时时间须在 1~60 秒之间")
        if kind in ("http", "nginx"):
            url = str(cfg.get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"{kind} 拨测必须配置 http(s):// 开头的 URL")
            host = urlsplit(url).hostname or ""
            if is_blocked_ip(host):
                raise ValueError("拨测目标不能是回环/链路本地/元数据/组播地址")
            if kind == "http":
                expect = cfg.get("expect_status")
                if expect is not None and not (100 <= int(expect) <= 599):
                    raise ValueError("期望状态码须在 100~599 之间")
        elif kind == "dns":
            if not str(cfg.get("domain") or "").strip():
                raise ValueError("dns 拨测必须配置域名")
        elif kind == "redis":
            # host/password 可空；port 可空（缺省 6379），配了就必须合法
            host = str(cfg.get("host") or "")
            if host and is_blocked_ip(host):
                raise ValueError("拨测目标不能是回环/链路本地/元数据/组播地址")
            port = cfg.get("port")
            if port is not None and not (1 <= int(port) <= 65535):
                raise ValueError("redis 端口须在 1~65535 之间")
        else:  # tcp
            port = cfg.get("port")
            if port is None or not (1 <= int(port) <= 65535):
                raise ValueError("tcp 拨测必须配置 1~65535 的端口")
        return self


class DeviceOut(BaseModel):
    id: int
    name: str
    ip: str
    type: str
    subtype: str = ""
    group_name: str
    location: str
    credential_id: int | None
    credential_name: str | None = None
    ssh_credential_id: int | None = None
    ssh_credential_name: str | None = None
    monitor_enabled: bool
    status: str
    sys_descr: str
    sys_object_id: str
    last_latency_ms: int | None
    last_seen: datetime | None
    last_checked: datetime | None
    probe_config: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}


# ---- 自动发现 ----


class ScanIn(BaseModel):
    ranges: str = Field(min_length=1)
    credential_id: int | None = None  # 可选：对存活主机再探 SNMP 识别


class ImportIn(BaseModel):
    ips: list[str] = Field(min_length=1)
    type: str = "other"
    group_name: str = ""
    location: str = ""
    credential_id: int | None = None


class DiscoveryJobOut(BaseModel):
    id: int
    ranges: str
    status: str
    total: int
    done: int
    results: list[dict]
    error: str
    created_at: datetime
    finished_at: datetime | None


# ---- 总览 ----


class OverviewOut(BaseModel):
    total: int
    online: int
    offline: int
    unknown: int
    by_type: dict[str, int]
