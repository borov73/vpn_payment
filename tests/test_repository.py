from dataclasses import replace
from decimal import Decimal

import pytest

from tests.conftest import T0
from tests.test_blockchain import BLOCK_HASH, HASH
from vpn_payment.database.models import Quote, VerifiedTransfer
from vpn_payment.database.repository import Repository, StaleChange
from vpn_payment.services.tracker import Tracker


def transfer(config, **kwargs):
    return replace(
        VerifiedTransfer(
            HASH,
            config.sender_address,
            config.recipient_addresses[0],
            2_000_000_000_000_000,
            10,
            BLOCK_HASH,
            T0,
            100,
        ),
        **kwargs,
    )


async def valued(repo, config, price="2500"):
    await repo.save_transfer(transfer(config))
    await repo.apply_quote(HASH, Quote("bitstamp", T0, Decimal(price)))


async def test_duplicate_transaction_and_frozen_quote(repo, config):
    assert await repo.save_transfer(transfer(config))
    assert not await repo.save_transfer(transfer(config))
    await repo.apply_quote(HASH, Quote("bitstamp", T0, Decimal("2500")))
    await repo.apply_quote(HASH, Quote("coinbaseexchange", T0, Decimal("5000")))
    rows = await repo.transfers()
    assert len(rows) == 1 and rows[0]["estimated_eur"] == "5.00"
    assert rows[0]["exchange"] == "bitstamp"


async def test_adjustment_preserves_original_and_audit(repo, config):
    await valued(repo, config)
    await repo.adjust(HASH, Decimal("6.50"), "Провайдер уточнил сумму", 42, "5.00")
    row = await repo.transfer(HASH)
    assert row["effective_eur"] == "6.50" and row["estimated_eur"] == "5.00"
    await repo.adjust(HASH, None, "Вернуть рыночную оценку", 42, "6.50")
    row = await repo.transfer(HASH)
    assert row["effective_eur"] == "5.00"
    audit = await repo.rows("SELECT * FROM adjustments ORDER BY id")
    assert len(audit) == 2 and audit[0]["actor"] == 42


async def test_stale_adjustment_and_settings_are_rejected(repo, config):
    await valued(repo, config)
    with pytest.raises(StaleChange):
        await repo.adjust(HASH, Decimal("9"), "stale edit", 42, "4.00")
    await repo.change_settings({"price": "5"}, 42, 1)
    with pytest.raises(StaleChange):
        await repo.change_settings({"price": "6"}, 42, 1)
    assert (await repo.settings())["price"] == "5"


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", "1.001", "1000001"])
async def test_invalid_adjustments_rejected(repo, config, bad):
    await valued(repo, config)
    with pytest.raises(ValueError):
        await repo.adjust(HASH, Decimal(bad), "reason", 42, "5.00")


async def test_unpriced_payment_not_silently_zero_and_manual_estimate(repo, config):
    await repo.save_transfer(transfer(config))
    tracker = Tracker(config, repo)
    snapshot = await tracker.snapshot(T0)
    assert snapshot.unpriced == 1 and snapshot.result.balance == -4
    assert snapshot.result.preliminary and snapshot.result.start == T0
    await repo.adjust(HASH, Decimal("5.00"), "Ручное уточнение", 42, None)
    snapshot = await tracker.snapshot(T0)
    assert snapshot.unpriced == 0 and snapshot.result.balance == 1
    # A later original quote is retained without replacing the explicit adjustment.
    await repo.apply_quote(HASH, Quote("bitstamp", T0, Decimal("3000")))
    row = await repo.transfer(HASH)
    assert row["estimated_eur"] == "6.00" and row["effective_eur"] == "5.00"


async def test_pending_or_orphaned_not_in_balance(repo, config):
    await valued(repo, config)
    for status in ("pending", "orphaned", "excluded", "failed"):
        await repo.set_status(HASH, status)
        snapshot = await Tracker(config, repo).snapshot(T0)
        assert snapshot.result.balance == 0 and snapshot.result.start is None


async def test_sqlite_persists_restart_and_backup(repo, config, tmp_path):
    await valued(repo, config)
    await repo.change_settings({"start_at": int(T0.timestamp()), "start_confirmed": 1}, 42, 1)
    await repo.reserve_notification("event")
    await repo.save_scan("live", 10, 20, BLOCK_HASH)
    backup_path = await repo.backup(tmp_path / "backup.sqlite3")
    other = Repository(backup_path)
    await other.open(config)
    try:
        assert len(await other.transfers()) == 1
        assert (await other.rules()).start_confirmed
        assert not await other.reserve_notification("event")
        assert (await other.scan("live"))["cursor"] == 20
    finally:
        await other.close()
    await repo.close()
    await repo.open(config)
    assert (await repo.transfer(HASH))["effective_eur"] == "5.00"


async def test_backup_never_overwrites(repo, tmp_path):
    destination = tmp_path / "copy.sqlite3"
    await repo.backup(destination)
    with pytest.raises(ValueError, match="overwrite"):
        await repo.backup(destination)
    with pytest.raises(ValueError, match="different"):
        await repo.backup(repo.path)


async def test_euro_rounding_once_and_wei_exact(repo, config):
    await repo.save_transfer(transfer(config, value_wei=1_000_000_000_000_001))
    await repo.apply_quote(HASH, Quote("bitstamp", T0, Decimal("3995")))
    row = await repo.transfer(HASH)
    assert row["value_wei"] == "1000000000000001"
    assert row["raw_eur"] == "3.995000000000003995"
    assert row["estimated_eur"] == "4.00"


async def test_foreign_minute_quote_is_rejected(repo, config):
    from datetime import timedelta

    await repo.save_transfer(transfer(config))
    with pytest.raises(ValueError, match="minute"):
        await repo.apply_quote(HASH, Quote("bitstamp", T0 + timedelta(minutes=1), Decimal("2500")))
    assert not await repo.rows("SELECT * FROM quotes")


async def test_changed_wallet_cannot_reuse_db(repo, config):
    await repo.close()
    changed = config.model_copy(update={"sender_address": "0x" + "11" * 20})
    with pytest.raises(ValueError, match="separate"):
        await repo.open(changed)


async def test_original_quote_replaced_only_on_reincluded_block(repo, config):
    from datetime import timedelta

    await valued(repo, config)
    await repo.save_transfer(
        transfer(config, timestamp=T0 + timedelta(minutes=1), block_hash="0x" + "ff" * 32)
    )
    row = await repo.transfer(HASH)
    assert row["estimated_eur"] is None
    assert len(await repo.rows("SELECT * FROM quotes")) == 1


async def test_settings_recompute(repo, config):
    from datetime import timedelta

    await valued(repo, config)
    await repo.change_settings({"start_at": int(T0.timestamp()), "start_confirmed": 1}, 42, 1)
    tracker = Tracker(config, repo)
    assert (await tracker.snapshot(T0)).result.balance == 1
    await repo.change_settings({"price": "2.00", "period_days": 10}, 42, 2)
    assert (await tracker.snapshot(T0)).result.expires_at == T0 + timedelta(days=20)
    assert len(await repo.rows("SELECT * FROM settings_audit")) == 2


async def test_continuous_setting_and_audit_persist_without_changing_original_quote(repo, config):
    await valued(repo, config, "1765")
    await repo.change_settings(
        {"billing_mode": "continuous", "start_at": int(T0.timestamp()), "start_confirmed": 1}, 42, 1
    )
    assert (await Tracker(config, repo).snapshot(T0)).result.balance == Decimal("-0.47")
    original = await repo.transfer(HASH)
    await repo.close()
    await repo.open(config)
    assert (await repo.settings())["billing_mode"] == "continuous"
    assert (await repo.transfer(HASH))["quote_id"] == original["quote_id"]
    await repo.adjust(HASH, Decimal("4"), "Уточнение оценки", 42, "3.53")
    assert (await Tracker(config, repo).snapshot(T0)).result.balance == 0
    assert (await repo.transfer(HASH))["estimated_eur"] == "3.53"
    assert (
        '"billing_mode": "continuous"'
        in (await repo.rows("SELECT change_json FROM settings_audit"))[0]["change_json"]
    )


async def test_schema_v1_migration_keeps_data_and_makes_private_backup(config, tmp_path):
    import sqlite3

    from vpn_payment.database.models import SCHEMA

    legacy = """CREATE TABLE settings (
      id INTEGER PRIMARY KEY CHECK(id=1), price TEXT NOT NULL, period_days INTEGER NOT NULL,
      start_at INTEGER, start_confirmed INTEGER NOT NULL DEFAULT 0,
      notifications_enabled INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1
    );"""
    with sqlite3.connect(config.database_path) as db:
        db.executescript(legacy + SCHEMA)
        db.execute("INSERT INTO settings VALUES(1,'4.00',30,NULL,0,1,7)")
        db.execute("INSERT INTO notifications(key,state,created_at) VALUES('existing','sent',1)")
        db.execute("PRAGMA user_version=1")
    migrated = Repository(config.database_path)
    try:
        await migrated.open(config)
        assert (await migrated.settings())["revision"] == 7
        with pytest.raises(ValueError, match="Legacy"):
            await migrated.rules()
        assert not await migrated.reserve_notification("existing")
        assert (await migrated.rows("PRAGMA user_version"))[0]["user_version"] == 3
    finally:
        await migrated.close()
    backups = list(tmp_path.glob("*-before-v3-*.sqlite3"))
    assert len(backups) == 1 and backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert "billing_mode" not in [row[1] for row in db.execute("PRAGMA table_info(settings)")]
        assert db.execute("SELECT revision FROM settings").fetchone()[0] == 7
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


async def test_unknown_future_schema_is_rejected_without_downgrade(config):
    import sqlite3

    with sqlite3.connect(config.database_path) as db:
        db.execute("PRAGMA user_version=4")
    repository = Repository(config.database_path)
    with pytest.raises(ValueError, match="newer"):
        await repository.open(config)
    with sqlite3.connect(config.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 4


async def test_v2_backup_is_readonly_and_upgrade_preserves_records(repo, config, tmp_path):
    import sqlite3

    await valued(repo, config)
    await repo.reserve_notification("existing-event")
    before = await repo.transfers()
    await repo.close()
    with sqlite3.connect(config.database_path) as db:
        db.execute("ALTER TABLE notifications DROP COLUMN retry_at")
        db.execute("PRAGMA user_version=2")
    await repo.open(config, readonly=True)
    await repo.backup(tmp_path / "old-schema.sqlite3")
    await repo.close()
    with sqlite3.connect(config.database_path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert "retry_at" not in [row[1] for row in db.execute("PRAGMA table_info(notifications)")]
    await repo.open(config)
    assert await repo.transfers() == before
    assert not await repo.reserve_notification("existing-event")
    assert (await repo.rows("PRAGMA user_version"))[0]["user_version"] == 3
    backups = list(tmp_path.glob("*-before-v3-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
