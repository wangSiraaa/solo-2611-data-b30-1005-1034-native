"""工艺台异常类型。

业务拒绝（整组比例矛盾退校、工单无解等）以结构化结果返回；这里的异常
只用于格式损坏的情景文件与内部不变量。
"""


class CraftError(Exception):
    """情景/校样文件结构或取值非法。"""


class PlanningError(CraftError):
    """不存在满足全部硬约束的装窑/装箱方案。"""
