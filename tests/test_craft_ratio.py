"""精确有理区间与比例界的单元测试。"""

from __future__ import annotations

from fractions import Fraction

import pytest

from craftbench.errors import CraftError
from craftbench.ratio import Interval, as_fraction, fraction_text, ratio_bounds


def test_as_fraction_accepts_ints_and_fraction_strings():
    assert as_fraction(3) == Fraction(3)
    assert as_fraction("4/5") == Fraction(4, 5)
    assert as_fraction("7") == Fraction(7)
    assert fraction_text(Fraction(8, 10)) == "4/5"


def test_as_fraction_rejects_floats_and_decimals():
    with pytest.raises(CraftError):
        as_fraction(0.8)
    with pytest.raises(CraftError):
        as_fraction("0.8")
    with pytest.raises(CraftError):
        as_fraction("1e-2")
    with pytest.raises(CraftError):
        as_fraction(True)


def test_interval_closed_endpoints_and_intersection():
    a = Interval(Fraction(4, 5), Fraction(1))
    assert a.contains(Fraction(4, 5))   # 闭区间含下端
    assert a.contains(Fraction(1))      # 闭区间含上端
    assert a.intersect(Interval.point(Fraction(1))) == Interval.point(Fraction(1))
    assert a.intersect(Interval(Fraction(2), Fraction(3))) is None


def test_ratio_bounds_from_green_and_observed():
    # green=5, observed [4,5] => r in [4/5, 1]
    bounds = ratio_bounds(Fraction(5), Interval(Fraction(4), Fraction(5)))
    assert bounds == Interval(Fraction(4, 5), Fraction(1))


def test_inverted_interval_rejected():
    with pytest.raises(CraftError):
        Interval(Fraction(2), Fraction(1))
