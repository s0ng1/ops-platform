"""测试基座：临时 SQLite 库 + 种子用户 + TestClient。
默认跑 SQLite（零依赖）；设 OPS_TEST_DATABASE_URL=postgresql+psycopg://... 可对 PG 做方言回归
（示例：postgresql+psycopg://ops:ops_dev_2026@127.0.0.1:5432/ops_test，库不存在会自动建）。
"""
import os
import tempfile

import pytest

_pg_url = os.environ.get("OPS_TEST_DATABASE_URL")
if _pg_url:
    # 确保测试库存在：连默认库 ops 建库（docker 里 ops 用户即超级用户）
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    _url = make_url(_pg_url)
    _admin = create_engine(_url.set(database="ops"), isolation_level="AUTOCOMMIT")
    with _admin.connect() as conn:
        try:
            conn.execute(text(f'CREATE DATABASE "{_url.database}"'))
        except Exception:  # noqa: BLE001 - 已存在则忽略
            pass
    os.environ["OPS_DATABASE_URL"] = _pg_url
else:
    # 在导入 app 之前指向临时数据库与固定密钥
    _tmpdir = tempfile.mkdtemp(prefix="ops-test-")
    os.environ["OPS_DATABASE_URL"] = f"sqlite:///{_tmpdir}/test.db"
os.environ["OPS_FERNET_KEY"] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
# 测试大量用 127.0.0.1 起本地假服务做真拨测，关闭 SSRF 回环封禁（生产默认封）
os.environ["OPS_SSRF_BLOCK_LOOPBACK"] = "0"

from fastapi.testclient import TestClient  # noqa: E402

from app.core.database import Base, engine  # noqa: E402
from app.main import _init_db, app  # noqa: E402


@pytest.fixture(scope="session")
def client():
    if _pg_url:
        # PG 回归：每次重建空库，避免用例间数据残留。
        # 先删连续聚合（依赖 metrics 表，普通 drop_all 会被依赖挡住）
        from sqlalchemy import text

        with engine.begin() as conn:
            conn.execute(text("DROP MATERIALIZED VIEW IF EXISTS metrics_5m CASCADE"))
        Base.metadata.drop_all(engine)
    _init_db()
    return TestClient(app)


@pytest.fixture(scope="session")
def admin_token(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture(scope="session")
def viewer_token(client, admin_token):
    r = client.post(
        "/api/users",
        json={"username": "viewer1", "password": "viewer123", "role": "viewer"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (201, 409), r.text
    r = client.post("/api/auth/login", json={"username": "viewer1", "password": "viewer123"})
    return r.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}
