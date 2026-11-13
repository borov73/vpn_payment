from unittest.mock import AsyncMock

from tests.fake_chain import FakeChain
from vpn_payment.blockchain.client import RPCError
from vpn_payment.blockchain.monitor import Monitor


async def monitor_for(config, repo, **updates):
    config = config.model_copy(update={"confirmations": 4, **updates})
    chain = FakeChain(config)
    prices = AsyncMock(price_pending=AsyncMock(return_value=0))
    return Monitor(config, repo, chain, prices), chain


async def test_rpc_bootstrap_filters_addresses_failures_and_duplicates(config, repo):
    monitor, chain = await monitor_for(config, repo)
    chain.add(10)
    chain.add(20, recipient=config.recipient_addresses[1])
    chain.add(30, recipient="0x" + "11" * 20)
    chain.add(40, success=False)
    report = await monitor.sync()
    assert not report.errors and report.added == 2
    rows = await repo.transfers()
    assert len(rows) == 2 and not any(row["notify"] for row in rows)
    history = await repo.scan("history")
    assert history["cursor"] == history["target"] == 197
    assert not (await monitor.sync()).added
    assert len(await repo.transfers()) == 2
    # Only blocks with sender activity were fetched with full transactions.
    assert {n for n, full in chain.block_calls if full} == {10, 20, 30, 40}


async def test_new_payment_after_restart_uses_saved_live_cursor(config, repo):
    monitor, chain = await monitor_for(config, repo)
    await monitor.sync()
    chain.height = 220
    key = chain.add(205)
    restarted = Monitor(monitor.config, repo, chain, monitor.prices)
    report = await restarted.sync()
    assert report.added == 1 and not report.errors
    assert (await repo.transfer(key))["notify"] == 1
    assert (await repo.scan("live"))["cursor"] == 217


async def test_history_rpc_error_does_not_fake_complete_or_stop_live(config, repo):
    monitor, chain = await monitor_for(config, repo)
    chain.add(10)
    chain.fail_nonce_below = 20
    report = await monitor.sync()
    assert report.errors
    assert (await repo.scan("history"))["cursor"] == 0
    chain.height = 220
    key = chain.add(205)
    report = await monitor.sync()
    assert report.errors and (await repo.scan("history"))["cursor"] == 0
    assert (await repo.transfer(key))["status"] == "confirmed"
    assert await repo.meta("last_live_error") == ""


async def test_failed_rpc_preserves_cursor(config, repo):
    monitor, chain = await monitor_for(config, repo)
    await monitor.sync()
    saved = await repo.scan("live")
    chain.fail_all = True
    assert (await monitor.sync()).errors
    assert await repo.scan("live") == saved


async def test_concurrent_sync_is_rejected(config, repo):
    monitor, _ = await monitor_for(config, repo)
    async with monitor.lock:
        assert (await monitor.sync()).busy


async def test_manual_hash_can_import_pending_and_later_confirm(config, repo):
    monitor, chain = await monitor_for(config, repo)
    key = chain.add(199)
    report = await monitor.sync([key, key])
    assert report.added == 1
    assert (await repo.transfer(key))["status"] == "pending"
    chain.height = 210
    await monitor.sync()
    assert (await repo.transfer(key))["status"] == "confirmed"


async def test_bounded_chunk_resumes_without_skips(config, repo):
    monitor, chain = await monitor_for(config, repo, max_tx_blocks_per_sync=1)
    for number in (10, 20, 30):
        chain.add(number)
    first = await monitor.sync()
    assert first.added == 1 and (await repo.scan("history"))["cursor"] == 10
    await monitor.sync()
    await monitor.sync()
    await monitor.sync()
    assert len(await repo.transfers()) == 3
    assert (await repo.scan("history"))["cursor"] == 197


async def test_bounded_blocks_fallback_needs_no_historical_nonce(config, repo):
    monitor, chain = await monitor_for(
        config, repo, history_mode="manual", monitor_mode="blocks", max_blocks_per_sync=2
    )
    await monitor.sync()
    chain.height = 210
    chain.fail_nonce_below = 10**9
    chain.add(199)
    report = await monitor.sync()
    assert not report.errors and report.added == 1
    assert (await repo.scan("live"))["cursor"] == 199
    assert chain.nonce_calls == 0 and await repo.scan("history") is None


async def test_reorg_removes_payment_and_rescans_scope(config, repo):
    monitor, chain = await monitor_for(config, repo)
    key = chain.add(10)
    await monitor.sync()
    del chain.txs[key]
    chain.fork_from = 10
    replacement = chain.add(20)
    await monitor.sync()
    assert (await repo.transfer(key))["status"] == "orphaned"
    assert (await repo.transfer(replacement))["status"] == "confirmed"


async def test_adding_recipient_forces_history_rescan(config, repo):
    monitor, chain = await monitor_for(config, repo)
    third = "0x" + "11" * 20
    key = chain.add(10, recipient=third)
    await monitor.sync()
    assert not await repo.transfer(key)
    changed = monitor.config.model_copy(
        update={"recipient_addresses": (*config.recipient_addresses, third)}
    )
    other = Monitor(changed, repo, chain, monitor.prices)
    await other.sync()
    assert (await repo.transfer(key))["status"] == "confirmed"


async def test_recipient_change_backfills_past_initial_anchor_without_history(config, repo):
    monitor, chain = await monitor_for(config, repo, history_mode="manual")
    await monitor.sync()
    third = "0x" + "11" * 20
    chain.height = 220
    key = chain.add(205, recipient=third)
    await monitor.sync()
    assert not await repo.transfer(key)
    assert (await repo.scan("live"))["cursor"] == 217
    changed = monitor.config.model_copy(
        update={
            "history_mode": "nonce",
            "recipient_addresses": (*config.recipient_addresses, third),
        }
    )
    chain.height = 230
    report = await Monitor(changed, repo, chain, monitor.prices).sync()
    assert not report.errors
    assert (await repo.transfer(key))["status"] == "confirmed"
    assert (await repo.scan("history"))["target"] == 227


async def test_target_reorg_during_empty_nonce_range_does_not_advance_cursor(config, repo):
    monitor, chain = await monitor_for(config, repo)
    await monitor.sync()
    chain.height = 220
    original = chain.block
    calls = 0

    async def changing(number, full=False):
        nonlocal calls
        if number == 217:
            calls += 1
            if calls == 2:
                chain.fork_from = 201
        return await original(number, full)

    chain.block = changing
    report = await monitor.sync()
    assert report.errors
    assert (await repo.scan("live"))["cursor"] == 197


async def test_history_keeps_both_addresses_but_live_only_uses_active(config, repo):
    monitor, chain = await monitor_for(config, repo)
    old = chain.add(10, recipient=config.recipient_addresses[0])
    new = chain.add(20, recipient=config.active_recipient_addresses[0])
    assert (await monitor.sync()).added == 2
    assert await repo.transfer(old) and await repo.transfer(new)
    chain.height = 220
    ignored = chain.add(205, recipient=config.recipient_addresses[0])
    accepted = chain.add(206, recipient=config.active_recipient_addresses[0])
    report = await monitor.sync()
    assert not report.errors and report.added == 1
    assert not await repo.transfer(ignored)
    assert ignored not in chain.receipt_calls
    assert (await repo.transfer(accepted))["notify"] == 1
    assert (await repo.transfer(old))["status"] == "confirmed"


async def test_unavailable_old_receipt_does_not_block_new_payments(config, repo):
    monitor, chain = await monitor_for(config, repo)
    old = chain.add(10)
    await monitor.sync()
    chain.receipt_calls.clear()
    chain.fail_receipts.add(old)
    chain.height = 220
    new = chain.add(205)
    report = await monitor.sync()
    assert not report.errors and report.added == 1
    assert (await repo.transfer(new))["status"] == "confirmed"
    assert old not in chain.receipt_calls
    assert (await repo.scan("live"))["cursor"] == 217


async def test_recent_audit_error_isolated_from_live_and_pricing(config, repo):
    monitor, chain = await monitor_for(config, repo)
    recent = chain.add(197)
    await monitor.sync()
    chain.fail_receipts.add(recent)
    chain.height = 204
    new = chain.add(201)
    report = await monitor.sync()
    assert report.errors and report.added == 1
    assert (await repo.transfer(new))["status"] == "confirmed"
    assert (await repo.scan("live"))["cursor"] == 201
    assert await repo.meta("last_live_error") == ""
    assert await repo.meta("last_reconcile_error")
    assert monitor.prices.price_pending.await_count == 2
    chain.fail_receipts.clear()
    await monitor.sync()
    assert await repo.meta("last_reconcile_error") == ""


async def test_history_rescan_and_manual_import_cannot_credit_future_retired_address(config, repo):
    monitor, chain = await monitor_for(config, repo)
    old = chain.add(10, recipient=config.recipient_addresses[0])
    await monitor.sync()
    chain.height = 220
    ignored = chain.add(205, recipient=config.recipient_addresses[0])
    await monitor.sync()
    assert not await repo.transfer(ignored)
    await repo.set_meta("history_reset_pending", "1")
    await monitor.sync([ignored])
    assert not await repo.transfer(ignored)
    assert (await repo.transfer(old))["status"] == "confirmed"


async def test_reorg_rechecks_manual_payment_outside_automatic_history(config, repo):
    from datetime import timedelta

    from tests.conftest import T0

    monitor, chain = await monitor_for(config, repo, history_start=T0 + timedelta(seconds=98))
    imported = chain.add(5)
    await monitor.sync([imported])
    assert (await repo.scan("history"))["start_block"] == 50
    assert (await repo.transfer(imported))["status"] == "confirmed"
    del chain.txs[imported]
    chain.fork_from = 5
    await monitor.sync()
    assert (await repo.transfer(imported))["status"] == "orphaned"


async def test_failed_full_reorg_audit_is_retried_after_restart(config, repo):
    monitor, chain = await monitor_for(config, repo, history_mode="manual")
    imported = chain.add(5)
    await monitor.sync([imported])
    del chain.txs[imported]
    chain.fork_from = 5
    original = chain.block

    async def unavailable(number, full=False):
        if number == 5:
            raise RPCError("Old block unavailable")
        return await original(number, full)

    chain.block = unavailable
    assert (await monitor.sync()).errors
    assert await repo.meta("reconcile_full_pending") == "1"
    assert (await repo.transfer(imported))["status"] == "confirmed"
    await repo.close()
    await repo.open(config)
    restarted = Monitor(monitor.config, repo, chain, monitor.prices)
    # Failure remains visible even though the checkpoint was already reset.
    assert (await restarted.sync()).errors
    assert await repo.meta("last_reconcile_error")
    chain.block = original
    chain.height = 220
    assert not (await restarted.sync()).errors
    assert (await repo.transfer(imported))["status"] == "orphaned"
    assert await repo.meta("reconcile_full_pending") == "0"
    assert await repo.meta("last_reconcile_error") == ""


async def test_recent_failed_check_is_retried_after_payment_ages_out(config, repo):
    monitor, chain = await monitor_for(config, repo)
    key = chain.add(197)
    await monitor.sync()
    chain.fail_receipts.add(key)
    assert (await monitor.sync()).errors
    chain.height = 220
    chain.receipt_calls.clear()
    assert (await monitor.sync()).errors
    assert key in chain.receipt_calls
    chain.fail_receipts.clear()
    assert not (await monitor.sync()).errors
    assert await repo.meta("reconcile_full_pending") == "0"


async def test_retirement_and_reorg_preserve_recorded_live_payment(config, repo):
    monitor, chain = await monitor_for(
        config, repo, history_mode="manual", active_recipient_addresses=config.recipient_addresses
    )
    await monitor.sync()
    chain.height = 220
    key = chain.add(205, recipient=config.recipient_addresses[0])
    await monitor.sync()
    changed = monitor.config.model_copy(
        update={"active_recipient_addresses": config.active_recipient_addresses}
    )
    retired = Monitor(changed, repo, chain, monitor.prices)
    await retired.sync()
    chain.height = 230
    chain.fork_from = 210  # The valid payment at 205 is unaffected by this reorg.
    ignored = chain.add(225, recipient=config.recipient_addresses[0])
    for _ in range(2):
        assert not (await retired.sync()).errors
        assert (await repo.transfer(key))["status"] == "confirmed"
        assert not await repo.transfer(ignored)


async def test_retirement_boundary_includes_unconfirmed_bootstrap_history(config, repo):
    monitor, chain = await monitor_for(config, repo)
    old = chain.add(199, recipient=config.recipient_addresses[0])
    await monitor.sync()  # History covers <=197, retirement cutoff is 200.
    assert not await repo.transfer(old)
    chain.height = 210
    report = await monitor.sync()
    assert not report.errors
    assert (await repo.transfer(old))["status"] == "confirmed"
