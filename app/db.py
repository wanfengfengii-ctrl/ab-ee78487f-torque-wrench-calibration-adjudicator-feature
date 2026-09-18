"""应用内 SQLite 持久化：扭矩扳手校准档案与复核快照仓库。

两张表构成校准档案纵向闭环：

- ``wrench_profiles``：每把扭矩扳手一行，承载当前设备状态与连续不合格
  次数（状态迁移规则见 :mod:`app.calibration`）；
- ``calibration_records``：每次登记一行的复核快照（单次判定的完整字段，
  以 JSON 原子写入），``(wrench_sn, seq)`` 唯一，``seq`` 从 1 起按
  登记顺序递增。误登记不删除：以 ``is_valid`` / ``voided_at`` /
  ``void_reason`` 三列软作废，原快照 JSON 原样保留作为审计证据。

写入一致性：登记/作废均在**同一个 ``BEGIN IMMEDIATE`` 事务**内完成
「档案 upsert/update + 快照 insert/标记」，二者要么同时可见要么同时
回滚，不会出现有快照无档案、已作废但档案状态未重算等中间态；写事务
在库级互斥，并发写同一把扳手时 ``seq``、作废标记与连续计数不会交错。

读取一致性：查询在**同一个 deferred 只读事务**内读取档案行与历史，
WAL 下二者共享同一数据库快照。否则两条独立 SELECT 之间若夹入一次写
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
    is_valid      INTEGER NOT NULL DEFAULT 1 CHECK (is_valid IN (0, 1)),
    voided_at     TEXT,
    void_reason   TEXT,
    UNIQUE (wrench_sn, seq)
);

CREATE INDEX IF NOT EXISTS idx_calibration_records_wrench_seq
    ON calibration_records (wrench_sn, seq);
"""

#: 供旧库平滑升级：补齐作废三列（列已存在时跳过，对已建库幂等）。
#: ADD COLUMN 不带 CHECK：旧行与新写入的合法性由新鲜建表的 CHECK 与
#: 领域代码（仅写 0/1）共同保证。
_MIGRATIONS = (
    "ALTER TABLE calibration_records ADD COLUMN is_valid INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE calibration_records ADD COLUMN voided_at TEXT",
    "ALTER TABLE calibration_records ADD COLUMN void_reason TEXT",
)


@dataclass(frozen=True)
class ProfileRow:
    """档案当前状态行。"""

    wrench_sn: str
    status: str
    consecutive_fail_count: int
    created_at: str


@dataclass(frozen=True)
class StoredRecord:
    """一条已持久化的复核快照（含作废标记）。"""

    seq: int
    registered_at: str
    snapshot: dict[str, Any]
    is_valid: bool = True
    voided_at: str | None = None
    void_reason: str | None = None


class _RegistrationTransaction:
    """一次写事务内的操作（由 :class:`CalibrationRepository` 产出）。"""

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

        新登记恒为有效（``is_valid=1``，作废三列其余为空）。

        :returns: 本次登记在该扳手档案中的序号（从 1 起，含已作废记录
            继续向后编号，作废不复用序号）。
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
            " (wrench_sn, seq, registered_at, snapshot_json, is_valid)"
            " VALUES (?, ?, ?, ?, 1)",
            (
                wrench_sn,
                seq,
                registered_at,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            ),
        )
        return seq

    def get_record(self, wrench_sn: str, seq: int) -> StoredRecord | None:
        """在写事务内读取指定序号记录（含作废标记）；不存在返回 None。"""
        row = self._conn.execute(
            "SELECT seq, registered_at, snapshot_json, is_valid,"
            "       voided_at, void_reason"
            "  FROM calibration_records"
            "  WHERE wrench_sn = ? AND seq = ?",
            (wrench_sn, seq),
        ).fetchone()
        return None if row is None else _row_to_record(row)

    def list_records(self, wrench_sn: str) -> list[StoredRecord]:
        """在写事务内按登记顺序读取该扳手的全部记录（供作废后重放）。"""
        rows = self._conn.execute(
            "SELECT seq, registered_at, snapshot_json, is_valid,"
            "       voided_at, void_reason"
            "  FROM calibration_records WHERE wrench_sn = ?"
            "  ORDER BY seq ASC",
            (wrench_sn,),
        ).fetchall()
        return [_row_to_record(row) for row in rows]

    def mark_record_void(
        self, wrench_sn: str, seq: int, voided_at: str, reason: str
    ) -> bool:
        """把仍有效（``is_valid=1``）的目标记录原子标记为作废。

        仅写入作废标记列，``snapshot_json`` 等原字段一概不动。

        :returns: 是否确实作废了一条记录；目标不存在或已作废时为
            ``False``（调用方据此区分 404 与 409，且不更新档案）。
        """
        cursor = self._conn.execute(
            "UPDATE calibration_records"
            " SET is_valid = 0, voided_at = ?, void_reason = ?"
            " WHERE wrench_sn = ? AND seq = ? AND is_valid = 1",
            (voided_at, reason, wrench_sn, seq),
        )
        return cursor.rowcount == 1

    def update_profile_status(
        self, wrench_sn: str, new_status: str, new_fail_count: int
    ) -> None:
        """在同一写事务内把重算后的状态与计数写回档案行。"""
        self._conn.execute(
            "UPDATE wrench_profiles"
            " SET status = ?, consecutive_fail_count = ?"
            " WHERE wrench_sn = ?",
            (new_status, new_fail_count, wrench_sn),
        )


def _row_to_record(row: sqlite3.Row) -> StoredRecord:
    """把一行（须含作废三列）映射为 :class:`StoredRecord`。"""
    return StoredRecord(
        seq=row["seq"],
        registered_at=row["registered_at"],
        snapshot=json.loads(row["snapshot_json"]),
        is_valid=bool(row["is_valid"]),
        voided_at=row["voided_at"],
        void_reason=row["void_reason"],
    )


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
            # 旧库平滑升级：CREATE TABLE IF NOT EXISTS 不会给既有表加列，
            # 逐列补齐；列已存在时 OperationalError 即跳过（幂等）。
            for statement in _MIGRATIONS:
                try:
                    conn.execute(statement)
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc):
                        raise

    @contextmanager
    def registration_transaction(self) -> Iterator[_RegistrationTransaction]:
        """开启互斥写事务；正常退出提交，异常回滚。

        登记与作废共用此事务（见 :meth:`write_transaction` 别名）：库级
        ``BEGIN IMMEDIATE`` 互斥保证同一扳手的写不交错。
        """
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

    # 作废编排与登记共用同一互斥写事务类型，语义上是“写事务”。
    write_transaction = registration_transaction

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
                "SELECT seq, registered_at, snapshot_json, is_valid,"
                "       voided_at, void_reason"
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
        records = [_row_to_record(row) for row in rows]
        return profile, records
