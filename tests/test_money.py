import pytest
from app.money import percent_discount


def test_rounds_half_up_with_integers_only():
    assert percent_discount(1999, 10) == 200   # 199.9 -> 200
    assert percent_discount(1005, 10) == 101   # 100.5 -> 101 (half up)
    assert percent_discount(1004, 10) == 100   # 100.4 -> 100
    assert percent_discount(1, 10) == 0


def test_discount_never_exceeds_subtotal():
    assert percent_discount(999, 100) == 999
    assert percent_discount(0, 50) == 0


def test_rejects_nonsense_inputs():
    with pytest.raises(ValueError):
        percent_discount(100, 101)
    with pytest.raises(ValueError):
        percent_discount(-1, 10)
