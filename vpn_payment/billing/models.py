from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must include timezone (use UTC)")
    return value.astimezone(UTC)


def money(value: Decimal | str) -> Decimal:
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("Amount must be finite")
    with localcontext() as ctx:
        ctx.prec = 60
        return result.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Payment:
    tx_hash: str
    timestamp: datetime
    eur: Decimal


@dataclass(frozen=True)
class BillingRules:
    price: Decimal = Decimal("4.00")
    period_days: int = 30
    start: datetime | None = None
    start_confirmed: bool = False


@dataclass(frozen=True)
class BillingResult:
    as_of: datetime
    start: datetime | None
    preliminary: bool
    balance: Decimal
    next_charge: datetime | None
    expires_at: datetime | None
    missing: Decimal

    @property
    def deficit(self) -> Decimal:
        """Bookkeeping shortfall, never a claim about the provider's actual debt."""
        return max(ZERO, -self.balance)
