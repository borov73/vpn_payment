import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.test_blockchain import BLOCK_HASH
from vpn_payment.blockchain.monitor import SyncReport
from vpn_payment.services.scheduler import notification_worker, sync_worker


@pytest.mark.parametrize(
    "condition,expected_delay", [("normal", 900), ("error", 30), ("backlog", 5), ("unexpected", 60)]
)
async def test_sync_worker_schedules_and_shuts_down(
    config, repo, monkeypatch, condition, expected_delay
):
    if condition == "backlog":
        await repo.save_scan("history", 1, 2, BLOCK_HASH, 100)
    report = SyncReport(errors=["RPC error"] if condition == "error" else [])
    monitor = SimpleNamespace(repo=repo, sync=AsyncMock(return_value=report))
    if condition == "unexpected":
        monitor.sync.side_effect = OSError("secret URL is not logged")
    notifier = AsyncMock()
    delays = []

    async def stop(delay):
        delays.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr("vpn_payment.services.scheduler.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await sync_worker(config, monitor, notifier)
    assert delays == [expected_delay]
    monitor.sync.assert_awaited_once()
    if condition == "unexpected":
        assert await repo.meta("last_live_error") == "Worker error: OSError"
    else:
        notifier.tick.assert_awaited_once()


async def test_notification_worker_survives_error_and_cancels(config, monkeypatch):
    notifier = AsyncMock()
    notifier.tick.side_effect = OSError("offline")

    async def stop(delay):
        assert delay == config.notification_interval_seconds
        raise asyncio.CancelledError

    monkeypatch.setattr("vpn_payment.services.scheduler.asyncio.sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        await notification_worker(config, notifier)


async def test_polling_shutdown_closes_sessions(config, repo, monkeypatch):
    from pydantic import SecretStr

    from vpn_payment.main import run_bot
    from vpn_payment.services.tracker import Tracker

    config = config.model_copy(
        update={"telegram_user_id": 42, "telegram_bot_token": SecretStr("42:TEST_ONLY")}
    )
    bot = SimpleNamespace(
        get_me=AsyncMock(), set_my_commands=AsyncMock(), session=SimpleNamespace(close=AsyncMock())
    )
    dispatcher = SimpleNamespace(
        include_router=lambda router: None,
        resolve_used_update_types=lambda: ["message", "callback_query"],
        start_polling=AsyncMock(),
        storage=SimpleNamespace(close=AsyncMock()),
    )
    monkeypatch.setattr("aiogram.Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr("aiogram.Dispatcher", lambda **kwargs: dispatcher)
    monitor = SimpleNamespace(lock=asyncio.Lock())
    await run_bot(config, repo, Tracker(config, repo), monitor)
    dispatcher.start_polling.assert_awaited_once()
    dispatcher.storage.close.assert_awaited_once()
    bot.session.close.assert_awaited_once()
