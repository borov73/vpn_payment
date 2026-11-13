import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from aiogram.exceptions import TelegramRetryAfter

from vpn_payment.blockchain.monitor import Monitor
from vpn_payment.bot.formatters import payment_notice, reminder_notice
from vpn_payment.bot.keyboards import menu
from vpn_payment.config import Settings
from vpn_payment.database.repository import Repository
from vpn_payment.services.tracker import Tracker

log = logging.getLogger(__name__)


class Notifications:
    """Persistent at-most-once delivery.

    Reserve before sending: a crash/ambiguous Telegram response cannot cause a
    duplicate on restart. The tradeoff is a potentially missed notification;
    uncertain reservations are never retried. Explicit Telegram flood-control
    rejections are retried after retry_after, with the delay persisted in SQLite.
    """

    def __init__(
        self,
        config: Settings,
        repo: Repository,
        tracker: Tracker,
        bot,
        monitor: Monitor | None = None,
    ):
        self.config, self.repo, self.tracker, self.bot, self.monitor = (
            config,
            repo,
            tracker,
            bot,
            monitor,
        )
        self.lock = asyncio.Lock()

    async def _send(self, key: str, text: str, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if not await self.repo.reserve_notification(key, now):
            return False
        try:
            message = await self.bot.send_message(
                chat_id=self.config.telegram_user_id, text=text, reply_markup=menu()
            )
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as error:
            await self.repo.finish_notification(
                key,
                None,
                "TelegramRetryAfter",
                retry_at=int(now.timestamp()) + max(1, error.retry_after),
            )
            log.warning("Telegram rate limit; notification deferred")
            return False
        except Exception as error:
            await self.repo.finish_notification(key, None, type(error).__name__)
            log.warning("Notification failed or uncertain (%s)", type(error).__name__)
            return False
        await self.repo.finish_notification(key, message.message_id)
        return True

    async def _ready_for_reminders(self, now: datetime) -> bool:
        history, live = await self.repo.scan("history"), await self.repo.scan("live")
        acknowledged = bool(await self.repo.meta("history_ack_at"))
        history_ready = bool(
            history
            and history["cursor"] >= history["target"]
            and await self.repo.meta("history_reset_pending") != "1"
        )
        if (
            not live
            or not (history_ready or acknowledged)
            or await self.repo.meta("last_live_error", "")
            or await self.repo.meta("last_reconcile_error", "")
            or await self.repo.meta("reconcile_full_pending") == "1"
        ):
            return False
        target = await self.repo.meta("last_confirmed_target")
        last = await self.repo.meta("last_live_success")
        return bool(
            target
            and live["cursor"] >= int(target)
            and last
            and 0
            <= (now - datetime.fromisoformat(last)).total_seconds()
            <= self.config.sync_interval_seconds * 2 + 60
        )

    async def _tick(self, now: datetime) -> None:
        if not (await self.repo.settings())["notifications_enabled"]:
            return
        if await self.repo.meta("last_live_error", ""):
            return
        snapshot = await self.tracker.snapshot(now)
        r = snapshot.result
        ready = await self._ready_for_reminders(now)
        pending = await self.repo.payment_notifications(now)
        for tx in snapshot.transfers:
            if tx["tx_hash"] not in pending or tx["effective_eur"] is None:
                continue
            # Send the final receipt only after its historical EUR estimate exists.
            await self._send(
                "payment:" + tx["tx_hash"],
                payment_notice(snapshot, Decimal(tx["effective_eur"]), provisional=not ready),
                now,
            )
        if r.preliminary or snapshot.unpriced or r.start is None or not ready:
            return
        if r.deficit:
            return
        if not r.expires_at:
            return
        remaining = (r.expires_at - now).total_seconds() / 86400
        if remaining <= 0:
            return
        threshold = 1 if remaining <= 1 else 3 if remaining <= 3 else 7 if remaining <= 7 else None
        if threshold is None:
            return
        # The expiry includes every full period that existing funds can cover.
        await self._send(
            f"reminder:{int(r.expires_at.timestamp())}:{threshold}",
            reminder_notice(snapshot),
            now,
        )

    async def tick(self, now: datetime | None = None) -> None:
        if self.lock.locked() or (self.monitor and self.monitor.lock.locked()):
            return
        async with self.lock:
            if self.monitor:
                async with self.monitor.lock:
                    await self._tick(now or datetime.now(UTC))
            else:
                await self._tick(now or datetime.now(UTC))
