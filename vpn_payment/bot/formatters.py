import math
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from vpn_payment.billing.models import BillingResult
from vpn_payment.database.repository import date
from vpn_payment.services.tracker import Snapshot

DISPLAY_ZONE = ZoneInfo("Europe/Moscow")


def eur(value: Decimal | str) -> str:
    return f"{Decimal(value):.2f}".replace(".", ",") + " €"


def short_date(value: datetime) -> str:
    return value.astimezone(DISPLAY_ZONE).strftime("%d.%m.%Y")


def result_text(
    result: BillingResult,
    price: Decimal,
    days: int,
    provisional: bool = False,
) -> str:
    lines = ["💰 <b>Баланс VPN</b>", "", f"<b>{eur(result.balance)}</b>", ""]
    if provisional or result.preliminary:
        lines.append("⏳ Баланс и дата пополнения уточняются.")
    elif result.deficit:
        lines.append(f"⚠️ Для покрытия начислений добавить <b>{eur(result.deficit)}</b>.")
    elif result.expires_at:
        lines.extend(
            [
                f"📅 Пополнить до <b>{short_date(result.expires_at)}</b>",
                f"Нужно <b>{eur(result.missing)}</b> на следующие {days} дней.",
            ]
        )
    else:
        lines.append("📅 Дата пополнения пока не рассчитана.")
    lines.extend(["", f"<i>{eur(price)} / {days} дней</i>"])
    return "\n".join(lines)


def status_text(snapshot: Snapshot) -> str:
    return result_text(
        snapshot.result,
        snapshot.rules.price,
        snapshot.rules.period_days,
        provisional=bool(snapshot.unpriced),
    )


def history_text(rows: list[dict], page: int, pages: int) -> str:
    lines = ["📜 <b>Пополнения VPN</b>", ""]
    for row in rows:
        amount = (
            f"<b>+{eur(row['effective_eur'])}</b>"
            if row["effective_eur"] is not None
            else "⏳ сумма уточняется"
        )
        lines.append(f"{short_date(date(row['timestamp']))}  ·  {amount}")
    if not rows:
        lines.append("Пополнений пока нет.")
    if pages > 1:
        lines.extend(["", f"<i>{page + 1} / {pages}</i>"])
    return "\n".join(lines)


def after_next_charge(snapshot: Snapshot) -> Decimal | None:
    r = snapshot.result
    if r.next_charge is None:
        return None
    return r.balance - snapshot.rules.price


def payment_notice(snapshot: Snapshot, amount: Decimal, provisional: bool = False) -> str:
    r = snapshot.result
    lines = [
        "✅ <b>Пополнение учтено</b>",
        "",
        f"Поступило: <b>+{eur(amount)}</b>",
        f"Баланс: <b>{eur(r.balance)}</b>",
    ]
    residual = after_next_charge(snapshot)
    if snapshot.unpriced or r.preliminary or provisional:
        lines.append("⏳ Итоговый баланс ещё уточняется.")
    elif residual is not None:
        lines.append(f"После ближайшего списания: <b>{eur(residual)}</b>")
    return "\n".join(lines)


def reminder_notice(snapshot: Snapshot) -> str:
    r = snapshot.result
    days = max(1, math.ceil((r.expires_at - r.as_of).total_seconds() / 86400))
    return (
        f"🔔 <b>Скоро пополнение VPN</b>\n\n"
        f"Осталось: <b>{days} дн.</b>\n"
        f"📅 До <b>{short_date(r.expires_at)}</b>\n"
        f"Нужно добавить: <b>{eur(r.missing)}</b>\n\n"
        f"<i>На следующие {snapshot.rules.period_days} дней по расчёту.</i>"
    )
