from datetime import timedelta
from decimal import Decimal

import pytest

from tests.conftest import T0
from vpn_payment.billing.calculator import calculate
from vpn_payment.billing.models import BillingRules, Payment, money


def pay(eur, days=0, name="a"):
    return Payment(name, T0 + timedelta(days=days), Decimal(eur))


def rules(**kwargs):
    return BillingRules(start=T0, start_confirmed=True, **kwargs)


@pytest.mark.parametrize(
    "amount,balance,days,missing",
    [
        ("4", "0", 30, "4"),
        ("5", "1", 30, "3"),
        ("10", "6", 60, "2"),
        ("20", "16", 150, "4"),
        ("3.99", "-0.01", 0, "4.01"),
    ],
)
def test_full_periods_and_remaining_funds(amount, balance, days, missing):
    result = calculate([pay(amount)], rules(), T0)
    assert result.balance == Decimal(balance)
    assert result.expires_at == T0 + timedelta(days=days)
    assert result.missing == Decimal(missing)


def test_several_dates_payments_not_counted_early():
    payments = [pay("5"), pay("5", 20, "b"), pay("10", 40, "c")]
    early = calculate(payments, rules(), T0 + timedelta(days=10))
    late = calculate(payments, rules(), T0 + timedelta(days=45))
    assert early.balance == 1 and early.expires_at == T0 + timedelta(days=30)
    assert late.balance == 12 and late.expires_at == T0 + timedelta(days=150)


def test_same_day_payments_and_boundary():
    result = calculate([pay("2", name="a"), pay("2", name="b")], rules(), T0)
    assert result.balance == 0 and result.expires_at == T0 + timedelta(days=30)
    later = Payment("late", T0 + timedelta(hours=2), Decimal("2"))
    assert calculate([pay("2"), later], rules(), T0 + timedelta(hours=3)).balance == 0
    assert calculate([pay("4"), pay("4", 30, "b")], rules(), T0 + timedelta(days=30)).balance == 0


def test_one_microsecond_after_boundary_is_not_available_early():
    at = T0 + timedelta(days=30)
    payments = [pay("4"), Payment("late", at + timedelta(microseconds=1), Decimal("4"))]
    assert calculate(payments, rules(), at).balance == -4
    assert calculate(payments, rules(), at + timedelta(microseconds=1)).balance == 0


def test_unknown_start_preliminary_and_empty():
    empty = calculate([], BillingRules(), T0)
    inferred = calculate([pay("5")], BillingRules(), T0)
    assert empty.start is None and empty.next_charge is None
    assert inferred.start == T0 and inferred.preliminary


def test_settings_change_recomputes_from_events():
    payment = [pay("10")]
    old = calculate(payment, rules(), T0 + timedelta(days=15))
    new = calculate(payment, BillingRules(Decimal("5"), 10, T0, True), T0 + timedelta(days=15))
    assert old.expires_at == T0 + timedelta(days=60)
    assert new.expires_at == T0 + timedelta(days=20) and new.balance == 0


def test_no_daily_proration():
    result = calculate([pay("5")], rules(), T0 + timedelta(days=1))
    assert result.balance == 1 and result.expires_at == T0 + timedelta(days=30)
    assert result.missing == 3


def test_fractional_cent_policy_and_no_float_drift():
    assert money("3.995") == Decimal("4.00")
    assert money("3.9949") == Decimal("3.99")
    result = calculate([pay("0.10", name=str(i)) for i in range(40)], rules(), T0)
    assert result.balance == Decimal("0.00")


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "1.001"])
def test_invalid_payments_rejected(bad):
    with pytest.raises(ValueError):
        calculate([pay(bad)], rules(), T0)


def test_naive_time_rejected():
    with pytest.raises(ValueError, match="timezone"):
        calculate([], rules(), T0.replace(tzinfo=None))


@pytest.mark.parametrize("price,days", [("0", 30), ("1.001", 30), ("NaN", 30), ("4", 0)])
def test_invalid_rules_rejected(price, days):
    with pytest.raises(ValueError):
        calculate([], BillingRules(Decimal(price), days, T0, True), T0)
