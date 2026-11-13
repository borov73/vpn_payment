from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.conftest import T0
from tests.test_blockchain import BLOCK_HASH, HASH
from tests.test_repository import transfer
from vpn_payment.database.models import Quote
from vpn_payment.services.notifications import Notifications
from vpn_payment.services.tracker import Tracker


async def setup(repo, config, amount="4", now=None, notify=False):
    config = config.model_copy(update={"telegram_user_id": 42})
    await repo.save_transfer(
        transfer(config, value_wei=int(Decimal(amount) / 2500 * 10**18)), notify
    )
    await repo.apply_quote(HASH, Quote("bitstamp", T0, Decimal("2500")))
    await repo.change_settings({"start_at": int(T0.timestamp()), "start_confirmed": 1}, 42, 1)
    await repo.save_scan("history", 1, 100, BLOCK_HASH, 100)
    await repo.save_scan("live", 101, 200, BLOCK_HASH)
    await repo.set_meta("last_confirmed_target", "200")
    await repo.set_meta("last_live_success", (now or T0).isoformat())
    bot = AsyncMock(send_message=AsyncMock(return_value=SimpleNamespace(message_id=1)))
    return Notifications(config, repo, Tracker(config, repo), bot), bot


async def test_persistent_no_duplicate_after_restart(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now)
    await service.tick(now)
    await service.tick(now)
    assert bot.send_message.await_count == 1
    assert "7 дн." in bot.send_message.call_args.kwargs["text"]
    await repo.close()
    await repo.open(config)
    restarted = Notifications(service.config, repo, Tracker(config, repo), bot)
    await restarted.tick(now)
    assert bot.send_message.await_count == 1


async def test_all_reminder_thresholds_once(repo, config):
    service, bot = await setup(repo, config)
    for day in (23, 24, 27, 28, 29, 30, 31):
        now = T0 + timedelta(days=day)
        await repo.set_meta("last_live_success", now.isoformat())
        await service.tick(now)
    assert bot.send_message.await_count == 3
    keys = [
        row["key"].rsplit(":", 1)[1] for row in await repo.rows("SELECT key FROM notifications")
    ]
    assert set(keys) == {"7", "3", "1"}


async def test_advance_payment_postpones_reminder(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, amount="8", now=now)
    await service.tick(now)
    bot.send_message.assert_not_awaited()
    later = T0 + timedelta(days=53)
    await repo.set_meta("last_live_success", later.isoformat())
    await service.tick(later)
    assert bot.send_message.await_count == 1


async def test_new_payment_notice_not_repeated(repo, config):
    service, bot = await setup(repo, config, amount="5", notify=True)
    await service.tick(T0)
    await service.tick(T0)
    assert bot.send_message.await_count == 1
    text = bot.send_message.call_args.kwargs["text"]
    assert "+5,00 €" in text and "1,00 €" in text
    assert "0x" not in text and "BaseScan" not in text and "ETH" not in text


async def test_missing_quote_suppresses_expiry_guess(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now)
    other = transfer(config, tx_hash="0x" + "aa" * 32)
    await repo.save_transfer(other)
    await service.tick(now)
    bot.send_message.assert_not_awaited()


async def test_preliminary_start_suppresses_reminders(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now)
    await repo.change_settings({"start_confirmed": 0}, 42, 2)
    await service.tick(now)
    bot.send_message.assert_not_awaited()


async def test_incomplete_history_requires_explicit_ack(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now)
    await repo.save_scan("history", 1, 50, BLOCK_HASH, 100)
    await service.tick(now)
    bot.send_message.assert_not_awaited()
    await repo.set_meta("history_ack_at", now.isoformat())
    await service.tick(now)
    assert bot.send_message.await_count == 1
    assert (await repo.scan("history"))["cursor"] == 50  # acknowledgement is not completeness


@pytest.mark.parametrize(
    "condition", ["rpc_error", "stale", "backlog", "disabled", "audit_pending"]
)
async def test_unreliable_state_or_disabled_has_no_reminders(repo, config, condition):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now)
    if condition == "rpc_error":
        await repo.set_meta("last_live_error", "RPC unavailable")
    elif condition == "stale":
        await repo.set_meta("last_live_success", T0.isoformat())
    elif condition == "backlog":
        await repo.set_meta("last_confirmed_target", "300")
    elif condition == "audit_pending":
        await repo.set_meta("reconcile_full_pending", "1")
    else:
        await repo.change_settings({"notifications_enabled": 0}, 42, 2)
    await service.tick(now)
    bot.send_message.assert_not_awaited()


async def test_ambiguous_telegram_error_does_not_duplicate(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now)
    bot.send_message.side_effect = TimeoutError("uncertain")
    await service.tick(now)
    bot.send_message.side_effect = None
    await Notifications(service.config, repo, Tracker(config, repo), bot).tick(now)
    assert bot.send_message.await_count == 1
    assert (await repo.rows("SELECT state FROM notifications"))[0]["state"] == "failed"


async def test_reserved_before_crash_is_not_retried(repo, config):
    service, bot = await setup(repo, config)
    await repo.reserve_notification("uncertain-event")
    assert not await service._send("uncertain-event", "text")
    bot.send_message.assert_not_awaited()


async def test_only_pre_payment_thresholds_are_sent_without_extra_overdue_alerts(repo, config):
    now = T0 + timedelta(days=1)
    service, bot = await setup(repo, config, amount="3.53", now=now)
    await service.tick(now)
    await service.tick(now)
    bot.send_message.assert_not_awaited()


async def test_payment_receipt_includes_balance_after_next_charge(repo, config):
    service, bot = await setup(repo, config, amount="10", notify=True)
    await service.tick(T0)
    text = bot.send_message.call_args.kwargs["text"]
    assert "Поступило: <b>+10,00 €</b>" in text
    assert "Баланс: <b>6,00 €</b>" in text
    assert "После ближайшего списания: <b>2,00 €</b>" in text
    assert "0x" not in text
    assert len(text) < 260
    await service.tick(T0)
    assert bot.send_message.await_count == 1


async def test_payment_notification_waits_for_quote_without_losing_event(repo, config):
    now = T0 + timedelta(days=1)
    service, bot = await setup(repo, config, amount="4", now=now)
    tx = transfer(config, tx_hash="0x" + "aa" * 32, timestamp=now)
    await repo.save_transfer(tx, notify=True)
    await service.tick(now)
    bot.send_message.assert_not_awaited()
    assert not await repo.rows("SELECT * FROM notifications")
    await repo.apply_quote(tx.tx_hash, Quote("bitstamp", now, Decimal("2500")))
    restarted = Notifications(service.config, repo, Tracker(config, repo), bot)
    await restarted.tick(now)
    assert bot.send_message.await_count == 1
    assert "+5,00 €" in bot.send_message.call_args.kwargs["text"]


async def test_top_up_cancels_reminders_for_the_previous_due_date(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, amount="4", now=now)
    await service.tick(now)  # Seven-day reminder for the initial funded period.
    at = T0 + timedelta(days=24)
    tx = transfer(config, tx_hash="0x" + "aa" * 32, timestamp=at, value_wei=1_600_000_000_000_000)
    await repo.save_transfer(tx, notify=True)
    await repo.apply_quote(tx.tx_hash, Quote("bitstamp", at, Decimal("2500")))
    for day in (24, 27, 29):
        at = T0 + timedelta(days=day)
        await repo.set_meta("last_live_success", at.isoformat())
        await service.tick(at)
    assert bot.send_message.await_count == 2  # One reminder, one payment receipt.
    keys = [r["key"] for r in await repo.rows("SELECT key FROM notifications")]
    assert sum(key.startswith("reminder:") for key in keys) == 1


async def test_continuous_prepaid_future_period_does_not_remind_early(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, amount="8", now=now)
    await service.tick(now)
    bot.send_message.assert_not_awaited()
    later = T0 + timedelta(days=53)
    await repo.set_meta("last_live_success", later.isoformat())
    await service.tick(later)
    assert bot.send_message.await_count == 1
    assert "7 дн." in bot.send_message.call_args.kwargs["text"]


async def test_explicit_retry_after_survives_restart_and_waits_until_due(repo, config):
    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    service, bot = await setup(repo, config, amount="5", now=T0, notify=True)
    bot.send_message.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=42, text="mock"), message="Too Many Requests", retry_after=120
    )
    await service.tick(T0)
    assert bot.send_message.await_count == 1
    row = (await repo.rows("SELECT * FROM notifications"))[0]
    assert row["state"] == "failed" and row["retry_at"] == int(T0.timestamp()) + 120
    await repo.close()
    await repo.open(config)
    restarted = Notifications(service.config, repo, Tracker(config, repo), bot)
    bot.send_message.side_effect = None
    await restarted.tick(T0 + timedelta(seconds=119))
    assert bot.send_message.await_count == 1
    await restarted.tick(T0 + timedelta(seconds=120))
    assert bot.send_message.await_count == 2
    assert (await repo.rows("SELECT state,retry_at FROM notifications")) == [
        {"state": "sent", "retry_at": None}
    ]
    await restarted.tick(T0 + timedelta(seconds=121))
    assert bot.send_message.await_count == 2


async def test_audit_error_suppresses_reminder_without_blocking_payment_receipt(repo, config):
    now = T0 + timedelta(days=23)
    service, bot = await setup(repo, config, now=now, notify=True)
    await repo.set_meta("last_reconcile_error", "Receipt unavailable")
    await service.tick(now)
    assert bot.send_message.await_count == 1
    text = bot.send_message.call_args.kwargs["text"]
    assert "Пополнение учтено" in text and "уточняется" in text
    assert not any(
        row["key"].startswith("reminder:")
        for row in await repo.rows("SELECT key FROM notifications")
    )


async def test_sent_payments_are_not_requeued(repo, config):
    service, bot = await setup(repo, config, notify=True)
    await service.tick(T0)
    assert not await repo.payment_notifications(T0)
    assert bot.send_message.await_count == 1
