"""系统门面：持有一条 SQLite 连接，组合目录与台账两个独立服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db
from .catalog import CatalogService
from .conservation import ConservationService
from .curator import CuratorService
from .ledger import LedgerService


class LibrarySystem:
    def __init__(self, db_path: str = ":memory:") -> None:
        self.db_path = db_path
        self.conn = db.connect(db_path)
        self.catalog = CatalogService(self.conn)
        self.ledger = LedgerService(self.conn, self.catalog)
        # 保存员在台账真实副本/保留/取件之上建逐册光照履历；
        # 策展人读目录、台账占用与保存员双账排可兑现班表
        self.conservation = ConservationService(
            self.conn, self.catalog, self.ledger
        )
        self.curator = CuratorService(
            self.conn, self.catalog, self.conservation
        )

    @classmethod
    def reopen(cls, db_path: str) -> "LibrarySystem":
        """重开持久化库：schema 用 IF NOT EXISTS 幂等建立，历史数据原样可见。"""
        if db_path == ":memory:":
            raise ValueError("cannot reopen an in-memory database")
        if not Path(db_path).exists():
            raise FileNotFoundError(db_path)
        return cls(db_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "LibrarySystem":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
