"""Synthetic continuous subscription timeline and historical as-of balances."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from vpn_payment.billing.calculator import calculate
from vpn_payment.billing.models import BillingRules, Payment

START = datetime(2026, 1, 1, 12, tzinfo=UTC)
RULES = BillingRules(start=START, start_confirmed=True)
PAYMENTS = [
    Payment("first", START, Decimal("3.50")),
    Payment("second", START + timedelta(days=18), Decimal("4.50")),
    Payment("third", START + timedelta(days=46), Decimal("10.00")),
]


@pytest.mark.parametrize(
    "offset,balance",
    [
        (timedelta(0), "-0.50"),
        (timedelta(days=17), "-0.50"),
        (timedelta(days=18), "4.00"),
        (timedelta(days=30, microseconds=-1), "4.00"),
        (timedelta(days=30), "0.00"),
        (timedelta(days=46), "10.00"),
        (timedelta(days=60), "6.00"),
        (timedelta(days=70), "6.00"),
        (timedelta(days=90), "2.00"),
        (timedelta(days=120), "-2.00"),
    ],
)
def test_continuous_timeline(offset, balance):
    assert calculate(PAYMENTS, RULES, START + offset).balance == Decimal(balance)


def test_current_balance_and_forecast():
    result = calculate(PAYMENTS, RULES, START + timedelta(days=70))
    assert result.balance == Decimal("6.00") and result.deficit == 0
    assert result.missing == Decimal("2.00")
    assert result.next_charge == START + timedelta(days=90)
    assert result.expires_at == START + timedelta(days=120)


def test_future_payment_cannot_erase_historical_shortfall():
    at = START + timedelta(days=5)
    assert calculate(PAYMENTS[:1], RULES, at) == calculate(PAYMENTS, RULES, at)
    assert calculate(PAYMENTS, RULES, at).deficit == Decimal("0.50")


def test_late_payment_settles_deficit_without_shifting_anchor():
    payments = [
        Payment("early", START, Decimal("4")),
        Payment("late", START + timedelta(days=65), Decimal("10")),
    ]
    before = calculate(payments, RULES, START + timedelta(days=61))
    after = calculate(payments, RULES, START + timedelta(days=66))
    assert before.balance == -8
    assert after.balance == 2 and after.start == START
    assert after.expires_at == START + timedelta(days=90)


def test_empty_confirmed_session_still_accrues():
    result = calculate([], RULES, START + timedelta(days=31))
    assert result.balance == -8 and result.deficit == 8 and result.expires_at == START


def test_future_start_holds_cash_without_early_charges():
    start = START + timedelta(days=2)
    result = calculate([Payment("a", START, Decimal("5"))], replace(RULES, start=start), START)
    assert result.balance == 5 and result.next_charge == start
    assert result.expires_at == start + timedelta(days=30)
