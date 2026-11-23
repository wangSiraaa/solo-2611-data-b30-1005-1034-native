"""工艺台（陶套筒烧前工单）领域层。

本包在不改动既有 ``extcontract`` 合同/摄入工具的前提下，新增三件事：

* ``ratio``   —— 闭区间上的精确有理运算与候选比例筛选；
* ``model``   —— 款、件、校样行、承架与情景的解析/校验；
* ``voucher`` —— 可独查的工艺凭单（哪组比例为何留下/排除）；
* ``planner`` —— 装窑（分炉/共架）与装箱（套叠方向、箱位）联合工单。

所有数值都用 :class:`fractions.Fraction` 精确表达，绝不出现浮点。
"""

from .model import (
    Piece,
    ProofObservation,
    Rack,
    Scenario,
    Style,
    load_scenario,
    parse_scenario,
)
from .planner import (
    KilnAssignment,
    NestEdge,
    WorkOrder,
    plan_work_order,
)
from .ratio import Interval, RationalInput, as_fraction, fraction_text
from .voucher import CraftVoucher, build_voucher

__all__ = [
    "Interval",
    "RationalInput",
    "as_fraction",
    "fraction_text",
    "Style",
    "Piece",
    "ProofObservation",
    "Rack",
    "Scenario",
    "parse_scenario",
    "load_scenario",
    "CraftVoucher",
    "build_voucher",
    "KilnAssignment",
    "NestEdge",
    "WorkOrder",
    "plan_work_order",
]
