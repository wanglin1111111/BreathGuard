"""SQLite 持久化：建表 + 事件/告警入库 + 读取。

按《01 架构》§6 的表结构精简实现（Demo 只落地 events / alerts 两张核心表）。
原始 CSI 信号绝不入库——库里只有结构化事件，符合隐私设计。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .protocol import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,
    device_id   TEXT,
    ts          TEXT NOT NULL,
    type        TEXT NOT NULL,
    confidence  REAL,
    duration_s  INTEGER,
    zone        TEXT,
    features    TEXT,
    source      TEXT DEFAULT 'dataset_replay'
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events(type, ts);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id     TEXT PRIMARY KEY,
    event_id     TEXT REFERENCES events(event_id),
    created_at   TEXT NOT NULL,
    level        TEXT NOT NULL,
    state        TEXT NOT NULL,
    agent_reason TEXT,
    closed_at    TEXT,
    closed_by    TEXT,
    agent_source TEXT DEFAULT 'rule',
    alert_tag    TEXT DEFAULT '',
    suspected_cause TEXT DEFAULT '',
    elder_name   TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at);
"""

# 迁移：给旧表加列（如果不存在）
_MIGRATIONS = [
    "ALTER TABLE alerts ADD COLUMN agent_source TEXT DEFAULT 'rule'",
    "ALTER TABLE alerts ADD COLUMN alert_tag TEXT DEFAULT ''",
    "ALTER TABLE alerts ADD COLUMN suspected_cause TEXT DEFAULT ''",
    "ALTER TABLE alerts ADD COLUMN elder_name TEXT DEFAULT ''",
]


class Store:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        # 迁移：给旧表加列（忽略已存在的列）
        for sql in _MIGRATIONS:
            try:
                self._conn.execute(sql)
            except sqlite3.OperationalError:
                pass  # 列已存在
        self._conn.commit()
        # 初始化病历表
        from .medical import init_medical_db
        init_medical_db(self._conn)

    def insert_event(self, ev: Event) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO events "
            "(event_id, device_id, ts, type, confidence, duration_s, zone, features, source) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ev.event_id, ev.device_id, ev.ts, ev.type, ev.confidence,
             ev.duration_s, ev.zone, json.dumps(ev.features, ensure_ascii=False), ev.source),
        )
        self._conn.commit()

    def insert_alert(self, alert: dict[str, Any]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO alerts "
            "(alert_id, event_id, created_at, level, state, agent_reason, closed_at, closed_by, "
            "agent_source, alert_tag, suspected_cause, elder_name) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (alert["alert_id"], alert.get("event_id"), alert["created_at"],
             alert["level"], alert["state"], alert.get("agent_reason"),
             alert.get("closed_at"), alert.get("closed_by"),
             alert.get("agent_source", "rule"),
             alert.get("alert_tag", ""),
             alert.get("suspected_cause", ""),
             alert.get("elder_name", "")),
        )
        self._conn.commit()

    def recent_events(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["features"] = json.loads(d["features"]) if d["features"] else {}
            out.append(d)
        return out

    def reset(self) -> None:
        self._conn.executescript("DELETE FROM events; DELETE FROM alerts;")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


def open_store(db_path: str = "waveguard.db") -> Store:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return Store(db_path)
