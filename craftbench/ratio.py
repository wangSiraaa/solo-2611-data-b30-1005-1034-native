"""精确有理运算与闭区间。

成形比例是正有理数对 ``(横向比例, 纵向比例)``，分别乘坯件的横、纵内外尺寸。
校样实读给出的是烧后**闭区间** ``[lo, hi]``，因此比例上的约束也是闭区间：
若坯尺寸为 ``b``（正数），烧后实读为 ``[lo, hi]``，则真实比例 ``r`` 满足

    lo <= b * r <= hi   <=>   lo/b <= r <= hi/b

全部用 :class:`fractions.Fraction` 计算，端点可相等（点区间，如校正后的
``[5,5]``），绝不引入浮点或四舍五入。
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from .errors import CraftError

RationalInput = int | str | Fraction


def as_fraction(raw: RationalInput, *, where: str = "数值") -> Fraction:
    """把 JSON 侧的整数或最简分数字符串精确转为 :class:`Fraction`。

    拒收浮点（JSON 里出现 0.8 说明写端没有走精确合同）、非正数与非法字符串。
    """
    if isinstance(raw, bool):
        raise CraftError(f"{where}: 布尔不能当作有理数 {raw!r}")
    if isinstance(raw, int):
        value = Fraction(raw)
    elif isinstance(raw, Fraction):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text or any(ch in text for ch in ".eE "):
            raise CraftError(f"{where}: 必须是整数或 n/d 分数字符串，收到 {raw!r}")
        try:
            value = Fraction(text)
        except (ValueError, ZeroDivisionError):
            raise CraftError(f"{where}: 非法分数字符串 {raw!r}") from None
    else:
        raise CraftError(f"{where}: 不支持的有理表示 {type(raw).__name__}")
    return value


def require_positive(value: Fraction, *, where: str) -> Fraction:
    if value <= 0:
        raise CraftError(f"{where}: 必须为正有理数，收到 {fraction_text(value)}")
    return value


def fraction_text(value: Fraction) -> str:
    """最简有理数字符串（与 reader-records 的最小形式一致）。"""
    return str(value)


@dataclass(frozen=True)
class Interval:
    """闭区间 ``[lo, hi]``，端点为精确非负有理数，``lo <= hi``。"""

    lo: Fraction
    hi: Fraction

    def __post_init__(self) -> None:
        if self.lo > self.hi:
            raise CraftError(
                f"闭区间下端不得大于上端: {fraction_text(self.lo)} > "
                f"{fraction_text(self.hi)}"
            )
        if self.lo < 0:
            raise CraftError("烧后尺寸区间不允许负数")

    @classmethod
    def from_raw(cls, lo: RationalInput, hi: RationalInput, *, where: str = "区间") -> "Interval":
        return cls(as_fraction(lo, where=f"{where}.lo"), as_fraction(hi, where=f"{where}.hi"))

    @classmethod
    def point(cls, value: Fraction) -> "Interval":
        return cls(value, value)

    @property
    def is_point(self) -> bool:
        return self.lo == self.hi

    def contains(self, value: Fraction) -> bool:
        return self.lo <= value <= self.hi

    def intersect(self, other: "Interval") -> "Interval | None":
        """闭区间交；不相交（无公共点）返回 ``None``。"""
        lo = max(self.lo, other.lo)
        hi = min(self.hi, other.hi)
        if lo > hi:
            return None
        return Interval(lo, hi)

    def scale(self, factor: Fraction) -> "Interval":
        """``factor > 0`` 时整体乘 ``factor``，保持闭区间。"""
        return Interval(self.lo * factor, self.hi * factor)

    def to_json(self) -> dict:
        return {"lo": fraction_text(self.lo), "hi": fraction_text(self.hi)}


def ratio_bounds(green: Fraction, observed: Interval) -> Interval:
    """由坯尺寸 ``green`` 与烧后闭区间推出真实比例所在的闭区间。"""
    if green <= 0:
        raise CraftError("坯尺寸必须为正数")
    return Interval(observed.lo / green, observed.hi / green)
