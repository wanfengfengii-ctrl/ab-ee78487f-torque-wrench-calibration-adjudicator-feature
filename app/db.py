"""应用内 SQLite 持久化：扭矩扳手校准档案与复核快照仓库。

两张表构成校准档案纵向闭环：

- ``wrench_profiles``：每把扭矩扳手一行，承载当前设备状态与连续不合格
  次数（状态迁移规则见 :mod:`app.calibration`）；
- ``calibration_records``：每次登记一行的复核快照（单次判定的完整字段，
  以 JSON 原子写入），``(wrench_sn, seq)`` 唯一，``seq`` 从 1 起按
  登记顺序递增。

写入一致性：登记在**同一个 ``BEGIN IMMEDIATE`` 事务**内完成「档案 upsert
+ 快照 insert」，二者要么同时可见要么同时回滚，不会出现有快照无档案或
状态已迁移无快照的中间态；写事务在库级互斥，并发登记同一把扳手时
``seq`` 与连续计数也不会交错。

读取一致性：查询在**同一个 deferred 只读事务**内读取档案行与历史，
WAL 下二者共享同一数据库快照。否则两条独立 SELECT 之间若夹入一次登记
提交，就会出现读偏斜——页面看到的档案行仍是旧状态（在用/0），而历史
里已经出现新的不合格快照。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS wrench_profiles (
    wrench_sn              TEXT PRIMARY KEY,
    status                 TEXT NOT NULL,
    consecutive_fail_count INTEGER NOT NULL,
    created_at             TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calibration_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wrench_sn     TEXT NOT NULL REFERENCES wrench_profiles(wrench_sn),
    seq           INTEGER NOT NULL,
    registered_at TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    UNIQUE (wrench_sn, seq)
);

CREATE INDEX IF NOT EXISTS idx_calibration_records_wrench_seq
    ON calibration_records (wrench_sn, seq);
"""


@dataclass(frozen=True)
class ProfileRow:
    """档案当前状态行。"""

    wrench_sn: str
    status: str
    consecutive_fail_count: int
    created_at: str


@dataclass(frozen=True)
class StoredRecord:
    """一条已持久化的复核快照。"""

    seq: int
    registered_at: str
    snapshot: dict[str, Any]


class _RegistrationTransaction:
    """一次登记事务内的操作（由 :class:`CalibrationRepository` 产出）。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get_profile(self, wrench_sn: str) -> ProfileRow | None:
        row = self._conn.execute(
            "SELECT wrench_sn, status, consecutive_fail_count, created_at"
            "  FROM wrench_profiles WHERE wrench_sn = ?",
            (wrench_sn,),
        ).fetchone()
        if row is None:
            return None
        return ProfileRow(
            wrench_sn=row["wrench_sn"],
            status=row["status"],
            consecutive_fail_count=row["consecutive_fail_count"],
            created_at=row["created_at"],
        )

    def save_record(
        self,
        wrench_sn: str,
        registered_at: str,
        snapshot: dict[str, Any],
        new_status: str,
        new_fail_count: int,
    ) -> int:
        """在已持有的写事务内：upsert 档案状态并插入复核快照。

        :returns: 本次登记在该扳手档案中的序号（从 1 起）。
        """
        row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS max_seq"
            "  FROM calibration_records WHERE wrench_sn = ?",
            (wrench_sn,),
        ).fetchone()
        seq = row["max_seq"] + 1

        # 首次登记自动建档；后续登记仅更新状态两列（created_at 保持首次时刻）。
        self._conn.execute(
            "INSERT INTO wrench_profiles"
            " (wrench_sn, status, consecutive_fail_count, created_at)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(wrench_sn) DO UPDATE SET"
            "   status = excluded.status,"
            "   consecutive_fail_count = excluded.consecutive_fail_count",
            (wrench_sn, new_status, new_fail_count, registered_at),
        )
        self._conn.execute(
            "INSERT INTO calibration_records"
            " (wrench_sn, seq, registered_at, snapshot_json)"
            " VALUES (?, ?, ?, ?)",
            (
                wrench_sn,
                seq,
                registered_at,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )
        return seq


class CalibrationRepository:
    """SQLite 档案仓库：单文件、短连接、外键与 busy_timeout 开启。"""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        db_dir = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(db_dir, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def registration_transaction(self) -> Iterator[_RegistrationTransaction]:
        """开启互斥写事务；正常退出提交，异常回滚。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield _RegistrationTransaction(conn)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """开启 deferred 只读事务：事务内所有 SELECT 共享同一 WAL 快照。

        WAL 模式下读事务不阻塞写者（登记），写者也不阻塞读者；但同一
        事务内的多次读取看到的是**同一个一致性快照**，避免“档案行已旧、
        历史已新”的读偏斜（边登记边查询时的撕裂视图）。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_profile_with_history(
        self, wrench_sn: str
    ) -> tuple[ProfileRow, list[StoredRecord]] | None:
        """读取档案当前状态与按登记顺序排列的完整历史；未知编号返回 None。

        档案行与历史在**同一只读事务**内读取，二者来自同一数据库快照，
        保证响应中的 ``status`` / ``consecutive_fail_count`` 与 ``history``
        始终相互自洽。
        """
        with self.read_transaction() as conn:
            prow = conn.execute(
                "SELECT wrench_sn, status, consecutive_fail_count, created_at"
                "  FROM wrench_profiles WHERE wrench_sn = ?",
                (wrench_sn,),
            ).fetchone()
            if prow is None:
                return None
            rows = conn.execute(
                "SELECT seq, registered_at, snapshot_json"
                "  FROM calibration_records WHERE wrench_sn = ?"
                "  ORDER BY seq ASC",
                (wrench_sn,),
            ).fetchall()

        profile = ProfileRow(
            wrench_sn=prow["wrench_sn"],
            status=prow["status"],
            consecutive_fail_count=prow["consecutive_fail_count"],
            created_at=prow["created_at"],
        )
        records = [
            StoredRecord(
                seq=row["seq"],
                registered_at=row["registered_at"],
                snapshot=json.loads(row["snapshot_json"]),
            )
            for row in rows
        ]
        return profile, records
