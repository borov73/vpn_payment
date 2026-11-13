from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from vpn_payment.billing.calculator import calculate
from vpn_payment.billing.models import BillingResult, BillingRules, Payment
from vpn_payment.config import Settings
from vpn_payment.database.repository import Repository, date


@dataclass(frozen=True)
class Snapshot:
    result: BillingResult
    rules: BillingRules
    transfers: list[dict]
    unpriced: int


class Tracker:
    def __init__(self, config: Settings, repo: Repository):
        self.config, self.repo = config, repo

    async def transfers(self) -> list[dict]:
        return [
            row
            for row in await self.repo.transfers()
            if row["recipient"] in self.config.recipient_addresses
            and row["sender"] == self.config.sender_address
        ]

    async def snapshot(
        self,
        as_of: datetime | None = None,
        *,
        rules: BillingRules | None = None,
        adjustment: tuple[str, Decimal | None] | None = None,
    ) -> Snapshot:
        now = as_of or datetime.now(UTC)
        rules = rules or await self.repo.rules()
        rows = await self.transfers()
        if adjustment is not None:
            tx_hash, amount = adjustment
            rows = [
                {
                    **row,
                    "effective_eur": str(amount) if amount is not None else row["estimated_eur"],
                }
                if row["tx_hash"] == tx_hash
                else row
                for row in rows
            ]
        confirmed = [
            row for row in rows if row["status"] == "confirmed" and date(row["timestamp"]) <= now
        ]
        if rules.start is None and confirmed:
            rules = replace(
                rules, start=date(min(row["timestamp"] for row in confirmed)), start_confirmed=False
            )
        payments = [
            Payment(row["tx_hash"], date(row["timestamp"]), Decimal(row["effective_eur"]))
            for row in confirmed
            if row["effective_eur"] is not None
        ]
        result = calculate(payments, rules, now)
        return Snapshot(result, rules, rows, sum(row["effective_eur"] is None for row in confirmed))
