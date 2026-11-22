"""合并书目下的预约先后与副本兑现。

模块划分：
- catalog   ：带修订的书目别名目录（不可变修订、单向别名边、多跳规范书目）
- ledger    ：独立的实物图书预约/取件台账（等待票据、保留、取件、回执）
- rights    ：权利室（副本印次/版税权段、取件认领、去重认列借次、复核）
- royalties ：逐作者逐期结算（单开/共池枚举、预算保版、幂等过账、结转）
- db        ：SQLite 连接、schema 与幂等基础设施
"""

from .errors import IdempotencyConflict, LibraryError, Rejection
from .system import LibrarySystem

__all__ = ["LibrarySystem", "LibraryError", "Rejection", "IdempotencyConflict"]
