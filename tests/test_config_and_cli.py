import logging
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from tests.conftest import ACTIVE_RECIPIENTS, RECIPIENTS, SENDER, settings
from vpn_payment.config import Settings, address
from vpn_payment.logging_setup import RedactingFormatter
from vpn_payment.main import execute, parser, process_lock


def test_address_validation_and_checksum():
    expected = "0x52908400098527886E0F7030069857D2E4169EE7"
    assert address(SENDER) == expected
    for value in ("0x1234", "0x" + "gg" * 20, "name.eth", "" * 20, "11" * 20):
        with pytest.raises(ValueError):
            address(value)


def test_comma_separated_config_and_secrets_not_in_validation_errors():
    config = settings(
        recipient_addresses=",".join([*RECIPIENTS, RECIPIENTS[0]]),
        price_exchanges="bitstamp,coinbaseexchange",
        subscription_start="",
    )
    assert len(config.recipient_addresses) == 2 and config.subscription_start is None
    with pytest.raises(ValidationError) as error:
        settings(base_rpc_url="private-test-token")
    assert "private-test-token" not in str(error.value)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"monthly_price_eur": "0"},
        {"monthly_price_eur": "1.001"},
        {"period_days": 0},
        {"confirmations": 0},
        {"telegram_user_id": -42},
        {"price_exchanges": "kraken"},
        {"history_start": "2026-08-01"},
    ],
)
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValidationError):
        settings(**kwargs)


def test_token_and_rpc_are_redacted_from_tracebacks():
    formatter = RedactingFormatter(["FAKE_TOKEN", "https://rpc.example/FAKE_KEY"])
    try:
        raise RuntimeError("FAKE_TOKEN https://rpc.example/FAKE_KEY")
    except RuntimeError:
        import sys

        record = logging.LogRecord("test", logging.ERROR, "", 1, "FAKE_TOKEN", (), sys.exc_info())
    text = formatter.format(record)
    assert "FAKE_TOKEN" not in text and "FAKE_KEY" not in text
    assert "[REDACTED]" in text


def test_process_lock_excludes_other_syncs(tmp_path):
    with process_lock(tmp_path / "state.sqlite3"):
        with pytest.raises(ValueError, match="Другой процесс"):
            with process_lock(tmp_path / "state.sqlite3"):
                pass
    with process_lock(tmp_path / "state.sqlite3"):
        pass


async def test_cli_init_status_backup_without_secrets(config, tmp_path, capsys):
    from vpn_payment.database.repository import Repository

    repo = Repository(config.database_path)
    await repo.open(config)
    await repo.close()
    assert await execute(parser().parse_args(["status"]), config) == 0
    backup = tmp_path / "backup.sqlite3"
    assert await execute(parser().parse_args(["backup", "--output", str(backup)]), config) == 0
    assert backup.exists()
    assert "Баланс VPN" in capsys.readouterr().out


async def test_run_requires_real_bot_config(config):
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
        await execute(parser().parse_args(["run"]), config)


async def test_rpc_retries_then_succeeds(config, monkeypatch):
    from vpn_payment.blockchain.client import BaseClient

    client = BaseClient(config.model_copy(update={"rpc_retries": 2}))
    monkeypatch.setattr("vpn_payment.blockchain.client.asyncio.sleep", AsyncMock())
    call = AsyncMock(side_effect=[OSError("fail"), 8453])
    try:
        assert await client._call(call, "eth_chainId") == 8453
        assert call.await_count == 2
    finally:
        await client.close()


def test_readonly_rpc_adapter_has_no_write_method():
    from vpn_payment.blockchain.client import BaseClient

    methods = [name for name in BaseClient.__dict__ if not name.startswith("_")]
    assert not any("send" in name or "sign" in name for name in methods)


@pytest.mark.parametrize("command", ["backup", "status"])
async def test_read_commands_do_not_create_missing_source(config, tmp_path, command):
    args = [command]
    destination = tmp_path / "copy.sqlite3"
    if command == "backup":
        args += ["--output", str(destination)]
    with pytest.raises(ValueError, match="does not exist"):
        await execute(parser().parse_args(args), config)
    assert not config.database_path.exists() and not destination.exists()


async def test_offline_doctor_does_not_create_database(config):
    assert await execute(parser().parse_args(["doctor"]), config) == 0
    assert not config.database_path.exists()


async def test_cli_maintenance_previews_and_audits_changes(repo, config, capsys):
    from tests.test_blockchain import HASH
    from tests.test_repository import valued

    await valued(repo, config)
    await repo.close()
    preview = [
        "configure",
        "--start",
        "2026-08-01T00:00:00Z",
        "--price",
        "5",
        "--acknowledge-history",
    ]
    assert await execute(parser().parse_args(preview), config) == 0
    await repo.open(config)
    assert (await repo.settings())["price"] == "4.00" and not await repo.meta("history_ack_at")
    await repo.close()
    assert await execute(parser().parse_args([*preview, "--apply"]), config) == 0
    await repo.open(config)
    assert (await repo.settings())["price"] == "5" and await repo.meta("history_ack_at")
    await repo.close()
    adjust = ["adjust", HASH, "6.50", "--reason", "Уточнение владельца"]
    assert await execute(parser().parse_args(adjust), config) == 0
    await repo.open(config)
    assert not await repo.rows("SELECT * FROM adjustments")
    await repo.close()
    assert await execute(parser().parse_args([*adjust, "--apply"]), config) == 0
    await repo.open(config)
    row = await repo.transfer(HASH)
    assert row["effective_eur"] == "6.50" and row["estimated_eur"] == "5.00"
    assert len(await repo.rows("SELECT * FROM adjustments")) == 1
    assert "Preview only" in capsys.readouterr().out


def test_active_addresses_are_normalized_and_subset_of_history():
    config = settings()
    assert config.active_recipient_addresses == tuple(address(v) for v in ACTIVE_RECIPIENTS)
    with pytest.raises(ValidationError, match="Active recipients"):
        settings(active_recipient_addresses="0x" + "11" * 20)


def test_personal_addresses_and_history_boundary_are_required():
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None)
    assert {e["loc"][0] for e in error.value.errors()} >= {
        "sender_address",
        "recipient_addresses",
        "active_recipient_addresses",
        "history_start",
    }


@pytest.mark.parametrize("apply", [False, True])
async def test_adjust_rejects_uncomputable_amount_before_writing(repo, config, apply):
    from tests.test_blockchain import HASH
    from tests.test_repository import valued

    await valued(repo, config)
    await repo.close()
    command = ["adjust", HASH, "1000000", "--reason", "Synthetic audit correction"]
    with pytest.raises(ValueError, match="supported range"):
        await execute(parser().parse_args(command + (["--apply"] if apply else [])), config)
    await repo.open(config)
    assert (await repo.transfer(HASH))["effective_eur"] == "5.00"
    assert not await repo.rows("SELECT * FROM adjustments")
    assert not list(config.database_path.parent.glob("*-before-adjust-*.sqlite3"))


@pytest.mark.parametrize("apply", [False, True])
async def test_configure_validates_actual_payments_before_writing(repo, config, apply):
    from tests.test_blockchain import HASH
    from tests.test_repository import valued

    await valued(repo, config)
    await repo.adjust(HASH, Decimal("1000"), "Synthetic large payment", 0, "5.00")
    await repo.close()
    command = [
        "configure",
        "--start",
        "2026-08-01T00:00:00Z",
        "--price",
        "0.01",
        "--period-days",
        "366",
    ]
    with pytest.raises(ValueError, match="supported range"):
        await execute(parser().parse_args(command + (["--apply"] if apply else [])), config)
    await repo.open(config)
    assert (await repo.settings())["price"] == "4.00"
    assert not await repo.rows("SELECT * FROM settings_audit")


async def test_adjust_is_in_euros_and_previews_the_result(repo, config, capsys):
    from tests.test_blockchain import HASH
    from tests.test_repository import valued

    await valued(repo, config)
    await repo.close()
    command = ["adjust", HASH, "5.00", "--reason", "Synthetic correction"]
    assert await execute(parser().parse_args(command), config) == 0
    output = capsys.readouterr().out
    assert "Баланс VPN" in output and "5.00 -> 5.00" in output
    assert await execute(parser().parse_args(command + ["--apply"]), config) == 0
    await repo.open(config)
    assert (await repo.transfer(HASH))["effective_eur"] == "5.00"
