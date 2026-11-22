"""合并书目下的预约先后、副本兑现与口述凭单。

模块划分：
- catalog   ：带修订的书目别名目录（不可变修订、单向别名边、多跳规范书目）
- ledger    ：独立的实物图书预约/取件台账（等待票据、保留、取件、回执）
- orals     ：真实取件上的读者口述凭单（阅读次序、同意公开、相邻过渡、旧版留存）
- exhibition：在凭单上求“起点→终点、必经各一次”的最省书链与受访场次
- db        ：SQLite 连接、schema 与幂等基础设施
"""

from .errors import IdempotencyConflict, LibraryError, Rejection
from .exhibition import ExhibitionService
from .orals import OralService
from .system import LibrarySystem

__all__ = [
    "LibrarySystem",
    "LibraryError",
    "Rejection",
    "IdempotencyConflict",
    "OralService",
    "ExhibitionService",
]
