"""Continuous bookkeeping: full periods from a confirmed start, no daily proration."""

from datetime import datetime, timedelta

from vpn_payment.billing.models import ZERO, BillingResult, BillingRules, Payment, money, utc


def calculate(payments: list[Payment], rules: BillingRules, as_of: datetime) -> BillingResult:
    now = utc(as_of)
    price = rules.price
    if not price.is_finite() or price <= 0 or money(price) != price:
        raise ValueError("Price must be positive EUR with at most two decimal places")
    if not 1 <= rules.period_days <= 366:
        raise ValueError("Period must be 1..366 days")
    for payment in payments:
        if not payment.eur.is_finite() or payment.eur < 0 or money(payment.eur) != payment.eur:
            raise ValueError("Payment must be nonnegative EUR with at most two decimal places")
    arrived = [payment for payment in payments if utc(payment.timestamp) <= now]
    total = sum((payment.eur for payment in arrived), ZERO)
    start = (
        utc(rules.start)
        if rules.start
        else min((utc(payment.timestamp) for payment in arrived), default=None)
    )
    preliminary = not rules.start_confirmed or rules.start is None
    if start is None:
        return BillingResult(now, None, True, total, None, None, max(ZERO, price - total))
    period = timedelta(days=rules.period_days)
    count = (now - start) // period + 1 if now >= start else 0
    balance = total - price * count
    reserved = max(0, int(balance // price))
    try:
        next_charge = start + count * period
        expires_at = start + int(total // price) * period
    except OverflowError:
        raise ValueError(
            "Calculated dates exceed the supported range; check amounts, price and period"
        ) from None
    return BillingResult(
        as_of=now,
        start=start,
        preliminary=preliminary,
        balance=balance,
        next_charge=next_charge,
        expires_at=expires_at,
        missing=price - (balance - reserved * price),
    )
