"""合并书目下的预约先后与副本兑现，以及保存/装订的残本整配。

模块划分：
- catalog：带修订的书目别名目录（不可变修订、单向别名边、多跳规范书目）
- ledger ：独立的实物图书预约/取件台账（等待票据、保留、取件、回执）
- bindery：残本整配（印次/版心/书帖/仿印、试排与最低费用联合求解）
- db     ：SQLite 连接、schema 与幂等基础设施
"""

from .bindery import BinderyService
from .errors import IdempotencyConflict, LibraryError, Rejection
from .system import LibrarySystem

__all__ = [
    "LibrarySystem",
    "BinderyService",
    "LibraryError",
    "Rejection",
    "IdempotencyConflict",
]
