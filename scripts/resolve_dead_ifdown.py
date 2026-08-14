"""存量噪音治理（一次性）：批量关闭「从未 up 过的接口」的 firing 接口 down 事件。

判定口径与告警引擎一致：metrics 表中该 (device_id, labels) 从未出现过 if_status=1
的历史点，即视为从未使用过的端口，其 firing 事件置 resolved 并在 note 追加
「噪音治理批量关闭」。

连接串：优先读环境变量 OPS_DATABASE_URL；缺省时从 deploy/.env 的 OPS_DB_PASSWORD
拼 dev 默认连接（ops@127.0.0.1:5432/ops）。不硬编码密码。

用法：backend/.venv/bin/python ../scripts/resolve_dead_ifdown.py
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

DEFAULT_ENV = Path(__file__).resolve().parent.parent / "deploy" / ".env"
NOTE = "噪音治理批量关闭"


def _db_url() -> str:
    url = os.environ.get("OPS_DATABASE_URL")
    if url:
        return url
    password = ""
    for line in DEFAULT_ENV.read_text().splitlines():
        if line.startswith("OPS_DB_PASSWORD="):
            password = line.split("=", 1)[1].strip()
            break
    if not password:
        sys.exit("未设置 OPS_DATABASE_URL 且 deploy/.env 中无 OPS_DB_PASSWORD")
    return f"postgresql+psycopg://ops:{password}@127.0.0.1:5432/ops"


def _labels_key(labels: dict) -> str:
    """与 app.alerting.engine.labels_key 口径一致。"""
    return json.dumps(labels or {}, sort_keys=True, ensure_ascii=False)


def main() -> None:
    engine = create_engine(_db_url())
    with engine.begin() as conn:
        def firing_major() -> int:
            return conn.execute(
                text("SELECT count(*) FROM alert_events WHERE status='firing' AND severity='major'")
            ).scalar_one()

        before = firing_major()

        # firing 的接口 down 事件
        events = conn.execute(
            text("SELECT id, device_id, labels FROM alert_events "
                 "WHERE status='firing' AND metric='if_status'")
        ).mappings().all()
        print(f"firing 的接口 down 事件：{len(events)} 条")
        if not events:
            print(f"firing major：{before} -> {before}（无需处理）")
            return

        # 曾 up（if_status=1）历史，一次性查回内存比对（与引擎同口径）
        device_ids = list({e["device_id"] for e in events})
        up_rows = conn.execute(
            text("SELECT DISTINCT device_id, labels FROM metrics "
                 "WHERE metric='if_status' AND value=1 AND device_id = ANY(:ids)"),
            {"ids": device_ids},
        ).all()
        up_keys = {(did, _labels_key(labels)) for did, labels in up_rows}

        dead_ids = [
            e["id"] for e in events
            if (e["device_id"], _labels_key(e["labels"])) not in up_keys
        ]
        print(f"其中「从未 up 过」的噪音事件：{len(dead_ids)} 条")
        if dead_ids:
            conn.execute(
                text(
                    "UPDATE alert_events SET status='resolved', resolved_at=:now, "
                    "note = CASE WHEN note='' THEN :note ELSE note || '；' || :note END "
                    "WHERE id = ANY(:ids)"
                ),
                {"now": datetime.now(), "note": NOTE, "ids": dead_ids},
            )
        after = firing_major()
        print(f"已批量关闭 {len(dead_ids)} 条；firing major：{before} -> {after}")


if __name__ == "__main__":
    main()
